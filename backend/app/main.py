import hmac
import json
import logging
import random
from contextlib import aclosing, asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sse_starlette.sse import EventSourceResponse

from app import redis as shared
from app import tracing
from app.after_sales.refund_router import router as refund_router
from app.after_sales.router import router as after_sales_router
from app.agent.llm import primary_model_name
from app.agent.runner import (
    ConversationNotFound,
    create_conversation,
    load_owned_conversation,
    new_conversation_id,
    run_agent_turn,
)
from app.auth import (
    ROLE_ADMIN,
    ROLE_CUSTOMER,
    Principal,
    bearer_token,
    mint_token,
    principal_from_request,
    require_admin,
    require_customer,
)
from app.config import get_settings
from app.db import session as db
from app.db.models import Conversation, Customer, Message, Order, ReasoningEvent, Refund
from app.db.seed import demo_data, seed_if_empty
from app.demo import order_out
from app.demo import router as demo_router
from app.events import broadcaster
from app.observability import (
    REJECTIONS,
    SSE_STREAMS,
    RequestContextMiddleware,
    configure_logging,
    configure_sentry,
    customer_id_var,
    log_event,
)
from app.schemas import (
    AdminStats,
    ChatRequest,
    ConversationDetail,
    ConversationSummary,
    MessageOut,
    OrderOut,
    ReasoningEventOut,
)
from app.workflow.runtime import workflow_lifespan

settings = get_settings()
logger = logging.getLogger("app")

STREAM_ERROR_MESSAGE = (
    "抱歉，处理请求时服务出现异常，请稍后重试。"
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    configure_sentry()
    tracing.configure()
    if settings.seed_on_startup:
        await seed_if_empty(create_tables=True)
    log_event(logger, "startup", env=settings.env, llm_provider=settings.llm_provider)
    async with workflow_lifespan():
        yield
    tracing.flush()
    await shared.close_redis()
    await db.dispose()


app = FastAPI(title="智能售后工作台", lifespan=lifespan)

# Docker/k8s 中，单页应用通过 nginx/ingress 的 /api 路由同源访问后端。
# 将客服组件嵌入其他来源的网站时才需要跨域访问，并应明确配置来源白名单。
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        # 前端在收到 429/503 时读取这些响应头，也可据此关联客服排查记录。
        expose_headers=["Retry-After", "X-Request-ID"],
        max_age=3600,
    )
app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_request_bytes)
app.include_router(demo_router)
app.include_router(after_sales_router)
app.include_router(refund_router)


@app.exception_handler(Exception)
async def _unhandled(_request: Request, exc: Exception):
    log_event(
        logger,
        "unhandled error",
        level=logging.ERROR,
        error_type=type(exc).__name__,
        error=str(exc)[:500],
    )
    return JSONResponse(status_code=500, content={"detail": "服务内部异常，请稍后重试。"})


# ---------------------------------------------------------------------------
# 健康检查与监控指标。
# ---------------------------------------------------------------------------


@app.get("/api/health")
@app.get("/api/health/live")
async def health() -> dict:
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "llm_key_configured": settings.has_llm_key,
    }


@app.get("/api/health/ready")
async def ready() -> Response:
    checks = {}
    try:
        async with db.SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:  # pragma: no cover - 在 k8s 环境中验证。
        checks["db"] = f"error: {type(exc).__name__}"
    try:
        await shared.get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["redis"] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, **checks})


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    if settings.metrics_token:
        given = bearer_token(request) or ""
        if not hmac.compare_digest(given.encode(), settings.metrics_token.encode()):
            raise HTTPException(status_code=401, detail="访问指标需要有效凭证。")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/meta")
async def meta() -> dict:
    """提供公开且不敏感的配置，供前端适配登录流程和模型信息。"""
    scripted = settings.llm_provider == "fake"
    return {
        "auth_mode": settings.auth_mode,
        "model": "scripted" if scripted else settings.llm_provider,
        "model_name": None if scripted else primary_model_name(),
        "demo": {
            "session_ttl_s": settings.demo_token_ttl_s,
            "data_ttl_hours": settings.demo_data_ttl_hours,
            # 清理沙箱时会归档聊天记录以供复查，见 maintenance/archive.py。
            "transcript_retention_days": settings.retention_days,
        }
        if settings.is_demo
        else None,
    }


# ---------------------------------------------------------------------------
# 仅开发模式（AUTH_MODE=dev）提供令牌签发接口。
# 生产环境下此路由返回 404，令牌由宿主网站签发。
# ---------------------------------------------------------------------------


class DevTokenRequest(BaseModel):
    customer_id: str | None = None
    admin: bool = False


@app.post("/api/dev/token")
async def dev_token(req: DevTokenRequest) -> dict:
    if settings.auth_mode != "dev" or settings.env == "prod":
        raise HTTPException(status_code=404, detail="未找到请求的数据。")
    if req.admin:
        return {"token": mint_token("dev-admin", ROLE_ADMIN), "role": ROLE_ADMIN}
    if not req.customer_id:
        raise HTTPException(status_code=400, detail="请选择客户。")
    async with db.SessionLocal() as session:
        if await session.get(Customer, req.customer_id) is None:
            raise HTTPException(status_code=404, detail="未找到该客户。")
    return {"token": mint_token(req.customer_id, ROLE_CUSTOMER), "role": ROLE_CUSTOMER}


@app.get("/api/dev/customers")
async def dev_customers() -> list[dict]:
    """为本地演示界面的客户选择器提供数据。"""
    if settings.auth_mode != "dev" or settings.env == "prod":
        raise HTTPException(status_code=404, detail="未找到请求的数据。")
    async with db.SessionLocal() as session:
        demo_ids = [row["id"] for row in demo_data()["customers"]]
        rows = (await session.scalars(select(Customer).where(Customer.id.in_(demo_ids))
                                     .order_by(Customer.id))).all()
    return [{"id": c.id, "name": c.name, "email": c.email} for c in rows]


# ---------------------------------------------------------------------------
# 客户聊天接口。
# ---------------------------------------------------------------------------


def _rate_limit_key(request: Request) -> str:
    principal = principal_from_request(request)
    if principal is not None:
        return f"sub:{principal.sub}"
    # Uvicorn 使用 --proxy-headers 启动；ingress/nginx 为可信代理时，
    # 此处可获得真实客户端 IP。
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def enforce_chat_rate_limit(request: Request) -> None:
    if principal_from_request(request) is None:
        # 缺失或无效令牌交由认证依赖返回 401。若按 IP 计入这类请求，
        # 共享 NAT/代理后的单个异常客户端可能耗尽同一 IP 下所有用户的额度。
        return
    allowed, reset_in = await shared.hit_rate_limit(
        f"chat:{_rate_limit_key(request)}", settings.chat_rate_limit
    )
    if not allowed:
        REJECTIONS.labels("rate_limited").inc()
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，当前限制为 {settings.chat_rate_limit}，请稍后重试。",
            headers={"Retry-After": str(reset_in)},
        )


@app.post("/api/chat", dependencies=[Depends(enforce_chat_rate_limit)])
async def chat(req: ChatRequest, principal: Principal = Depends(require_customer)):
    customer_id_var.set(principal.sub)
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空。")

    # 聊天入口：require_customer 先验证登录凭证，principal.sub 才是可信的客户身份。
    # 先检查消息长度，避免无效的大段输入进入模型调用。
    if len(req.message) > settings.max_message_chars:
        REJECTIONS.labels("too_long").inc()
        raise HTTPException(
            status_code=400,
            detail=(
                f"消息过长（{len(req.message)} 个字符），最多允许 {settings.max_message_chars} 个字符。"
            ),
        )

    # 继续旧会话前核对归属；他人会话与不存在的会话均返回 404，避免泄露会话信息。
    if req.conversation_id:
        try:
            await load_owned_conversation(principal.sub, req.conversation_id)
        except ConversationNotFound:
            raise HTTPException(status_code=404, detail="未找到该会话，请新建对话。") from None

        # 在打开 SSE 流之前检查消息上限，此时仍能返回正常的 HTTP 错误状态。
        async with db.SessionLocal() as session:
            existing = (
                await session.scalar(
                    select(func.count(Message.id)).where(
                        Message.conversation_id == req.conversation_id
                    )
                )
                or 0
            )
        if existing >= settings.max_conversation_turns:
            REJECTIONS.labels("turn_cap").inc()
            raise HTTPException(
                status_code=429,
                detail=(
                    f"当前会话已达到消息上限（{settings.max_conversation_turns} 条），请新建对话。"
                ),
            )
        cid = req.conversation_id
        is_new = False
    else:
        cid = new_conversation_id()
        is_new = True

    # Redis 控制正常入场；锁过期后的旧轮次由数据库消息版本拦住写入。
    lock_ttl = settings.turn_timeout_s + 30
    lock = await shared.acquire_turn_lock(cid, lock_ttl)
    if lock is None:
        REJECTIONS.labels("turn_in_progress").inc()
        raise HTTPException(
            status_code=409, detail="上一条消息仍在处理中，请稍候。"
        )

    # 再申请全局并发名额；名额不足时在调用模型前返回 503，前端可以稍后重试。
    slot = await shared.acquire_turn_slot(settings.max_concurrent_turns, lock_ttl)
    if slot is None:
        await shared.release_turn_lock(cid, lock)
        REJECTIONS.labels("overloaded").inc()
        raise HTTPException(
            status_code=503,
            detail="服务繁忙，请稍后重试。",
            # 随机等待时间让各客户端分散重试，避免同时再次挤满服务。
            headers={"Retry-After": str(random.randint(5, 15))},
        )

    if is_new:
        await create_conversation(principal.sub, cid)

    # SSE 持续发送工具调用、规则校验和最终回复等事件，不必等整轮结束才展示执行过程。
    async def event_stream():
        SSE_STREAMS.labels("chat").inc()
        try:
            async with aclosing(run_agent_turn(principal.sub, cid, req.message, lock_token=lock)) as stream:
                async for event in stream:
                    yield {"data": json.dumps(event)}
        except Exception as exc:
            # SSE 已发送响应头，无法再改 HTTP 状态；改为发送错误事件，异常细节留在日志。
            log_event(
                logger,
                "chat stream failed",
                level=logging.ERROR,
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            yield {"data": json.dumps({"kind": "error", "message": STREAM_ERROR_MESSAGE})}
        finally:
            SSE_STREAMS.labels("chat").dec()
            # 正常结束或抛出异常都释放并发名额和会话锁，避免后续请求一直被占用。
            await shared.release_turn_slot(slot)
            await shared.release_turn_lock(cid, lock)

    return EventSourceResponse(event_stream(), ping=15)


def _messages_out(messages) -> list[MessageOut]:
    return [
        MessageOut(id=m.id, role=m.role, content=m.content, created_at=m.created_at)
        for m in messages
    ]


@app.get("/api/me/conversations", response_model=list[ConversationSummary])
async def my_conversations(
    principal: Principal = Depends(require_customer),
    limit: int = Query(20, ge=1, le=100),
    before: datetime | None = None,
) -> list[ConversationSummary]:
    return await _conversation_summaries(limit, before, customer_id=principal.sub, nonempty=True)


@app.get("/api/me/conversations/{conversation_id}", response_model=ConversationDetail)
async def my_conversation(
    conversation_id: str, principal: Principal = Depends(require_customer)
) -> ConversationDetail:
    try:
        convo = await load_owned_conversation(principal.sub, conversation_id)
    except ConversationNotFound:
        raise HTTPException(status_code=404, detail="未找到该会话，请新建对话。") from None
    async with db.SessionLocal() as session:
        messages = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id)
            )
        ).all()
        events = (await session.scalars(
            select(ReasoningEvent).where(ReasoningEvent.conversation_id == conversation_id,
                                        ReasoningEvent.step_type != "trace")
            .order_by(ReasoningEvent.seq)
        )).all()
    return ConversationDetail(
        id=convo.id,
        customer_id=convo.customer_id,
        customer_name=None,
        messages=_messages_out(messages),
        events=[ReasoningEventOut(id=e.id, message_id=e.message_id, seq=e.seq,
                    step_type=e.step_type, node=e.node, payload=e.payload, created_at=e.created_at)
                for e in events],
    )


@app.get("/api/me/orders", response_model=list[OrderOut])
async def my_orders(principal: Principal = Depends(require_customer)) -> list[OrderOut]:
    async with db.SessionLocal() as session:
        orders = (
            await session.scalars(
                select(Order)
                .where(Order.customer_id == principal.sub)
                .order_by(Order.id)
                .limit(100)
            )
        ).all()
    return [order_out(o) for o in orders]


# ---------------------------------------------------------------------------
# 管理接口要求 role=admin。公开演示使用的受限管理员令牌只能查看对应客户的数据；
# 访问他人 ID 时返回 404，避免被探测。
# ---------------------------------------------------------------------------


async def _conversation_summaries(
    limit: int, before: datetime | None, customer_id: str | None = None, *, nonempty=False
) -> list[ConversationSummary]:
    msg_count = (
        select(func.count(Message.id))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    last_message = (
        select(Message.content)
        .where(Message.conversation_id == Conversation.id)
        .order_by(Message.id.desc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    stmt = (
        select(Conversation, Customer.name, msg_count, last_message)
        .outerjoin(Customer, Customer.id == Conversation.customer_id)
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.where(Conversation.created_at < before)
    if customer_id is not None:
        stmt = stmt.where(Conversation.customer_id == customer_id)
    if nonempty:
        stmt = stmt.where(msg_count > 0)
    async with db.SessionLocal() as session:
        rows = (await session.execute(stmt)).all()
    return [
        ConversationSummary(
            id=c.id,
            customer_id=c.customer_id,
            customer_name=name,
            created_at=c.created_at,
            message_count=count or 0,
            last_message=last,
        )
        for c, name, count, last in rows
    ]


@app.get("/api/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    before: datetime | None = Query(None, description="cursor: created_at of last row"),
    principal: Principal = Depends(require_admin),
) -> list[ConversationSummary]:
    return await _conversation_summaries(limit, before, customer_id=principal.scope)


def _visible(convo: Conversation | None, principal: Principal) -> bool:
    return convo is not None and (principal.scope is None or convo.customer_id == principal.scope)


@app.get("/api/admin/stats", response_model=AdminStats)
async def admin_stats(principal: Principal = Depends(require_admin)) -> AdminStats:
    """提供管理面板的概览统计，权限范围与会话列表一致。"""

    def scoped(stmt, conv_col):
        if principal.scope is None:
            return stmt
        owned = select(Conversation.id).where(Conversation.customer_id == principal.scope)
        return stmt.where(conv_col.in_(owned))

    async with db.SessionLocal() as session:
        conv_stmt = select(
            func.count(Conversation.id), func.coalesce(func.sum(Conversation.tokens_used), 0)
        )
        if principal.scope is not None:
            conv_stmt = conv_stmt.where(Conversation.customer_id == principal.scope)
        conversations, tokens = (await session.execute(conv_stmt)).one()
        messages = await session.scalar(
            scoped(select(func.count(Message.id)), Message.conversation_id)
        )
        decisions = (
            await session.execute(
                scoped(
                    select(Refund.decision, func.count(Refund.id)).group_by(Refund.decision),
                    Refund.conversation_id,
                )
            )
        ).all()
    return AdminStats(
        conversations=conversations or 0,
        messages=messages or 0,
        decisions={d: n for d, n in decisions},
        tokens_used=int(tokens or 0),
    )


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str, principal: Principal = Depends(require_admin)
) -> ConversationDetail:
    async with db.SessionLocal() as session:
        convo = await session.get(Conversation, conversation_id)
        if not _visible(convo, principal):
            raise HTTPException(status_code=404, detail="未找到该会话，请新建对话。")
        name = None
        if convo.customer_id:
            cust = await session.get(Customer, convo.customer_id)
            name = cust.name if cust else None
        messages = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id)
            )
        ).all()
        events = (
            await session.scalars(
                select(ReasoningEvent)
                .where(ReasoningEvent.conversation_id == conversation_id)
                .order_by(ReasoningEvent.seq)
            )
        ).all()
    return ConversationDetail(
        id=convo.id,
        customer_id=convo.customer_id,
        customer_name=name,
        messages=_messages_out(messages),
        events=[
            ReasoningEventOut(
                id=e.id,
                seq=e.seq,
                step_type=e.step_type,
                node=e.node,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ],
    )


@app.get("/api/conversations/{conversation_id}/stream")
async def stream_conversation(
    conversation_id: str, principal: Principal = Depends(require_admin)
):
    """为管理面板实时推送指定会话的执行事件。"""
    if principal.scope is not None:
        async with db.SessionLocal() as session:
            convo = await session.get(Conversation, conversation_id)
        if not _visible(convo, principal):
            raise HTTPException(status_code=404, detail="未找到该会话，请新建对话。")

    async def event_stream():
        SSE_STREAMS.labels("admin").inc()
        try:
            async for event in broadcaster.subscribe(conversation_id):
                yield {"data": json.dumps(event)}
        finally:
            SSE_STREAMS.labels("admin").dec()

    return EventSourceResponse(event_stream(), ping=15)
