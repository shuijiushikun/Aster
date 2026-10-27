"""售后工具；身份、当前请求、证据和业务写入均在服务端核验。"""

import hashlib
import time
from typing import Annotated, Literal

import jwt
from langchain_core.messages import HumanMessage
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.after_sales.intent import TurnIntent, requested_actions
from app.after_sales.references import resolve_order_reference
from app.after_sales.refund_service import RefundService
from app.after_sales.requests import RequestConflict
from app.after_sales.schemas import EvidenceRef, ExchangeInput
from app.after_sales.service import TicketError, TicketService
from app.agent.context import grounded_id
from app.auth import Principal
from app.config import get_settings
from app.db import session as db
from app.db.models import Conversation, Customer, KnowledgeDocument, Message, Order, Product
from app.mcp.auth import service_secret
from app.mcp.schemas import (
    CustomerProfile,
    Identifier,
    KnowledgeSearch,
    ReadResult,
    RefundDecision,
    RefundEligibility,
    TicketInfo,
    TicketPage,
    failure,
)

Query = Annotated[str, Field(min_length=1, max_length=2000)]
Target = Annotated[str, Field(max_length=500)]
Proof = Annotated[str, Field(max_length=16000)]
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


class OperationError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message


def identity(ctx):
    state = ctx.request_context.request.state
    return state.commerce_customer_id, state.commerce_turn


async def result(schema, operation, *, write=False):
    try:
        return schema(ok=True, data=await operation)
    except OperationError as error:
        return failure(error.code, error.message)
    except RequestConflict:
        return failure("REQUEST_CONFLICT", "同一办理请求的内容发生变化，请核实后重新提交。")
    except TicketError as error:
        return failure("NOT_FOUND" if error.status_code == 404 else "BUSINESS_REJECTED", error.message)
    except SQLAlchemyError:
        return failure("OUTCOME_UNKNOWN" if write else "MCP_UNAVAILABLE",
                       "业务服务暂不可用，请稍后查询办理状态。")


async def authorize_write(ctx, order_id, operation):
    customer_id, turn = identity(ctx)
    cid, mid = turn.get("conversation_id"), turn.get("message_id")
    async with db.SessionLocal() as session:
        conversation = await session.get(Conversation, cid) if cid else None
        message = await session.get(Message, mid) if mid else None
        if (conversation is None or conversation.customer_id != customer_id or message is None
                or message.conversation_id != cid or message.role != "user"):
            raise OperationError("ACTION_NOT_REQUESTED", "办理操作需要有效的当前客户请求。")
        action = "exchange" if operation == "create_exchange_request" else "refund"
        if "intent" in turn:
            intent = TurnIntent.model_validate(turn["intent"])
            if not intent.matches(message.content):
                raise OperationError("ACTION_NOT_REQUESTED", "本轮分类与当前消息不匹配，请重新确认申请。")
            actions = intent.actions
        else:
            # 保留旧调用兼容；新聊天已签名携带同一次分类，不在这里再次分类。
            actions = requested_actions(message.content)
        if action not in actions:
            raise OperationError("ACTION_NOT_REQUESTED", "当前消息未明确要求办理，请先确认申请。")
        previous = await session.scalar(select(Message).where(
            Message.conversation_id == cid, Message.role == "user", Message.id < mid,
        ).order_by(Message.id.desc()).limit(1))
        messages = [HumanMessage(previous.content)] if previous else []
        messages.append(HumanMessage(message.content))
        resolved = await resolve_order_reference(session, customer_id, message.content,
                                                 previous_text=previous.content if previous else None)
        if (grounded_id(messages, order_id, "ORD") is None
                and (resolved is None or resolved.casefold() != order_id.casefold())):
            raise OperationError("INVALID_ARGUMENT", "请明确本次要办理的唯一订单号。")
    key = hashlib.sha256(f"{cid}:{mid}:{operation}".encode()).hexdigest()
    return customer_id, cid, key


async def product_scope(customer_id, order_id=None, model_code=None):
    async with db.SessionLocal() as session:
        if order_id:
            order = await session.get(Order, order_id)
            if order is None or order.customer_id != customer_id:
                raise OperationError("NOT_FOUND", "未找到可访问的订单。")
            product = await session.get(Product, order.product_id) if order.product_id else None
        elif model_code:
            product = await session.scalar(select(Product).where(Product.model_code == model_code))
        else:
            return None, None
        if product is None or not product.is_active:
            raise OperationError("INVALID_ARGUMENT", "暂未核实有效商品型号，请补充商品信息。")
        return product.model_code, product.category


async def retrieve_knowledge(query, *, model_code=None, category=None, overview=False):
    from app.rag.retriever import KnowledgeRetriever
    from app.rag.schemas import RetrievalResult
    from app.rag.store import open_store

    try:
        async with open_store(get_settings()) as (store, client):
            return await KnowledgeRetriever(store, client).retrieve(
                query, model_code=model_code, category=category, overview=overview,
            )
    except Exception:
        return RetrievalResult(query=query, model_code=model_code, status="unavailable", reason="service_unavailable")


def register_business_tools(server, settings):
    secret = service_secret(settings)

    @server.tool(annotations=READ_ONLY)
    async def get_my_profile(ctx: Context) -> ReadResult[CustomerProfile]:
        """读取当前已登录客户的资料。"""
        async def run():
            async with db.SessionLocal() as session:
                customer = await session.get(Customer, identity(ctx)[0])
                if customer is None:
                    raise OperationError("NOT_FOUND", "未找到客户资料。")
                return CustomerProfile(customer_id=customer.id, name=customer.name,
                                       email=customer.email, loyalty_tier=customer.loyalty_tier)
        return await result(ReadResult[CustomerProfile], run())

    @server.tool(annotations=READ_ONLY)
    async def check_refund_eligibility(order_id: Identifier, ctx: Context) -> ReadResult[RefundEligibility]:
        """查询本人订单的退款资格，不办理退款。"""
        async def run():
            data = await RefundService().check(identity(ctx)[0], order_id)
            if data.get("error"):
                raise OperationError("NOT_FOUND", "未找到可访问的订单。")
            return RefundEligibility.model_validate(data)
        return await result(ReadResult[RefundEligibility], run())

    async def refund(ctx, order_id, reason=None):
        operation = "issue_refund" if reason is None else "escalate_to_human"
        owner, cid, key = await authorize_write(ctx, order_id, operation)
        data = await RefundService().execute(owner, order_id, cid, request_id=key, reason=reason)
        if data.get("error"):
            raise OperationError("NOT_FOUND" if data["error"] == "order_not_found" else "BUSINESS_REJECTED",
                                 data.get("message", "未找到可访问的订单。"))
        return RefundDecision.model_validate(data)

    @server.tool(annotations=WRITE)
    async def issue_refund(order_id: Identifier, ctx: Context) -> ReadResult[RefundDecision]:
        """客户明确申请后办理模拟退款；事务内复查资格，同一请求重放返回原结果。"""
        return await result(ReadResult[RefundDecision], refund(ctx, order_id), write=True)

    @server.tool(annotations=WRITE)
    async def escalate_to_human(order_id: Identifier, reason: Target, ctx: Context) -> ReadResult[RefundDecision]:
        """仅对符合规则且超过自动批准金额的退款申请转人工。"""
        return await result(ReadResult[RefundDecision], refund(ctx, order_id, reason), write=True)

    @server.tool(annotations=READ_ONLY)
    async def search_knowledge(query: Query, ctx: Context, order_id: Identifier | None = None,
                               model_code: Identifier | None = None,
                               overview: bool = False) -> ReadResult[KnowledgeSearch]:
        """检索售后资料；overview 用于浏览某型号的已有知识，不用于核实具体参数或换货建单。"""
        async def run():
            owner, turn = identity(ctx)
            code, category = await product_scope(owner, order_id, model_code)
            options = {"overview": True} if overview else {}
            evidence = await retrieve_knowledge(query, model_code=code, category=category, **options)
            for source in evidence.sources:
                source.excerpt = source.excerpt[:settings.context_evidence_max_chars]
            proof = None
            if evidence.status == "ok" and order_id and turn.get("message_id") and not overview:
                refs = [EvidenceRef(document_id=s.document_id, revision=s.revision,
                                    chunk_index=s.chunk_index, excerpt=s.excerpt[:300]).model_dump()
                        for s in evidence.sources]
                now = int(time.time())
                proof = jwt.encode({"sub": owner, **turn, "order_id": order_id,
                                    "model_code": code, "category": category, "sources": refs,
                                    "iss": "commerce-mcp", "aud": "exchange-evidence",
                                    "iat": now, "exp": now + 600}, secret, algorithm="HS256")
            return KnowledgeSearch(**evidence.model_dump(), evidence_token=proof)
        return await result(ReadResult[KnowledgeSearch], run())

    @server.tool(annotations=WRITE)
    async def create_exchange_request(order_id: Identifier, description: Query, ctx: Context,
                                      reason_type: Literal["size", "defect"] | None = None,
                                      requested_change: Target | None = None,
                                      evidence_token: Proof | None = None) -> ReadResult[TicketInfo]:
        """客户明确申请后提交或复用换货工单；必须提供本轮真实检索证据。"""
        async def run():
            owner, cid, key = await authorize_write(ctx, order_id, "create_exchange_request")
            principal = Principal(owner, "customer")
            service = TicketService()
            data = ExchangeInput(description=description, reason_type=reason_type,
                                 requested_change=requested_change)
            ticket = await service.recorded_exchange(principal, order_id, data, cid, key)
            if ticket is not None:
                detail = await service.detail(principal, ticket.id)
                return TicketInfo(id=ticket.id, order_id=ticket.order_id, status=detail.status,
                                  reference_code=detail.reference_code)
            code, category = await product_scope(owner, order_id)
            try:
                proof = jwt.decode(evidence_token or "", secret, algorithms=["HS256"],
                                   audience="exchange-evidence", issuer="commerce-mcp",
                                   options={"require": ["exp", "iat", "sub"]})
                expected = {"sub": owner, **identity(ctx)[1], "order_id": order_id,
                            "model_code": code, "category": category}
                if any(proof.get(k) != v for k, v in expected.items()) or not proof.get("sources"):
                    raise ValueError("evidence scope mismatch")
                refs = [EvidenceRef.model_validate(s) for s in proof["sources"]]
                async with db.SessionLocal() as session:
                    for ref in refs:
                        document = await session.get(KnowledgeDocument, ref.document_id)
                        if (document is None or document.status != "active" or document.revision != ref.revision
                                or (document.model_code is not None and
                                    (document.model_code != code or document.category != category))):
                            raise ValueError("evidence revision mismatch")
            except (jwt.PyJWTError, ValidationError, ValueError):
                raise OperationError("EVIDENCE_REQUIRED", "请先检索并取得本轮对应商品的有效资料。") from None
            ticket = await service.create_or_find_exchange(
                principal, order_id,
                ExchangeInput(description=description, reason_type=reason_type,
                              requested_change=requested_change, evidence_refs=refs),
                origin_conversation_id=cid, start_open=True, request_id=key,
            )
            detail = await service.detail(principal, ticket.id)
            return TicketInfo(id=ticket.id, order_id=ticket.order_id, status=detail.status,
                              reference_code=detail.reference_code)
        return await result(ReadResult[TicketInfo], run(), write=True)

    @server.tool(annotations=READ_ONLY)
    async def get_tickets(ctx: Context, ticket_id: Identifier | None = None,
                          order_id: Identifier | None = None) -> ReadResult[TicketPage]:
        """查询本人工单及受理凭证；查询不创建、审批或恢复工作流。"""
        async def run():
            principal = Principal(identity(ctx)[0], "customer")
            service = TicketService()
            if ticket_id:
                details = [await service.detail(principal, ticket_id)]
            else:
                rows = await service.list_tickets(principal, limit=20, order_id=order_id)
                details = [await service.detail(principal, t.id) for t in rows]
            return TicketPage(tickets=[TicketInfo(id=t.id, order_id=t.order_id, status=t.status,
                                                  reference_code=t.reference_code) for t in details])
        return await result(ReadResult[TicketPage], run())
