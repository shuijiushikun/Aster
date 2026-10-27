"""智能体可调用的工具。

智能体只能通过这些工具访问数据或记录退款。关键防线位于 `issue_refund`：
它会重新运行确定性规则引擎，只有规则允许时才能记录获批退款。模型生成的内容
以及恶意用户注入的文本都不能绕过该校验。`escalate_to_human` 同样遵循退款规则：
只有规则要求转人工的订单才可提交人工审核，每笔订单至多提交一次。

客户身份由请求中已验证的 JWT 确定，并在智能体运行前固定到 `ToolContext` 中；
模型只能查看或操作该客户的订单，不能自行决定当前客户是谁。

每次工具调用都使用独立的短时数据库会话，等待模型响应期间不占用数据库连接。

工具对象在每个进程内只创建一次（`TOOLS`）。每次请求的身份通过 LangGraph 的
RunnableConfig（`tool_config(ctx)`）传入，LangChain 将其注入 `config` 参数，
并在提供给模型的工具参数结构中隐藏该参数。每次请求重建 `@tool` 对象会重新
生成 Pydantic 参数结构，曾是压力测试中每轮对话最大的 CPU 开销来源。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from app.after_sales.intent import TurnIntent
from app.after_sales.locking import begin_order_write, lock_order
from app.after_sales.refund_service import ESCALATION_REASON_PREFIX as ESCALATION_REASON_PREFIX
from app.after_sales.refund_service import RefundService
from app.db import session as db
from app.db.models import Customer, Order
from app.localization import product_name

log = logging.getLogger(__name__)


# 阅读顺序：校验可信身份 → 读取本人订单 → 检查规则 → 写退款记录并提交事务。
# @tool 函数内的文档字符串会提供给模型作为工具说明，学习注释另用 # 补充。
@dataclass
class ToolContext:
    conversation_id: str
    verified_customer_id: str | None = None
    commerce_mcp_failed: bool = False
    message_id: int | None = None
    turn_intent: TurnIntent | None = None
    evidence_tokens: dict[str, str] = field(default_factory=dict)
    workflow_result: dict = field(default_factory=dict)




def _order_view(order: Order) -> dict:
    return {
        "order_id": order.id,
        "product_name": product_name(order.product_name),
        "category": order.category,
        "amount": float(order.amount),
        "status": order.status,
        "delivered_date": order.delivered_date.date().isoformat()
        if order.delivered_date
        else None,
        "is_final_sale": order.is_final_sale,
        "already_refunded": order.refunded,
    }


def tool_config(ctx: ToolContext) -> dict:
    """通过执行配置注入身份；该参数不会暴露给模型，客户也不能通过聊天修改。"""
    return {"configurable": {"tool_ctx": ctx}}


def _ctx(config: RunnableConfig) -> ToolContext:
    ctx = (config or {}).get("configurable", {}).get("tool_ctx")
    return ctx if isinstance(ctx, ToolContext) else ToolContext(conversation_id="")


def _build_tools() -> list:
    def _require_identity(ctx: ToolContext) -> str | None:
        if ctx.verified_customer_id is None:
            return json.dumps(
                {
                    "error": "identity_not_verified",
                    "message": "当前会话尚未验证客户身份。",
                }
            )
        return None

    async def _get_owned_order(
        ctx: ToolContext, session, order_id: str, for_update: bool = False
    ) -> tuple[Order | None, str | None]:
        stmt = select(Order).where(Order.id == order_id)
        if for_update:
            await begin_order_write(session)
            order = await lock_order(session, order_id)
        else:
            order = await session.scalar(stmt)
        if order is None:
            return None, json.dumps(
                {"error": "order_not_found", "order_id": order_id}
            )
        if order.customer_id != ctx.verified_customer_id:
            # 他人订单与不存在的订单返回相同错误，防止通过枚举订单号探知其他客户数据。
            # 运营日志仍记录越权尝试，但模型与客户无法获取该订单详情。
            log.warning(
                "cross-customer order access refused",
                extra={"order_id": order_id, "customer_id": ctx.verified_customer_id},
            )
            return None, json.dumps({"error": "order_not_found", "order_id": order_id})
        return order, None

    @tool
    async def get_my_profile(config: RunnableConfig) -> str:
        """读取已认证客户的资料，包括姓名、邮箱和会员等级。

        客户已经登录，无需再询问其身份。
        """
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        async with db.SessionLocal() as session:
            customer = await session.get(Customer, ctx.verified_customer_id)
        if customer is None:
            return json.dumps({"found": False, "message": "未找到客户资料。"})
        return json.dumps(
            {
                "found": True,
                "customer_id": customer.id,
                "name": customer.name,
                "email": customer.email,
                "loyalty_tier": customer.loyalty_tier,
            }
        )

    @tool
    async def list_orders(config: RunnableConfig) -> str:
        """列出属于当前已登录客户的订单。"""
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        async with db.SessionLocal() as session:
            orders = (
                await session.scalars(
                    select(Order)
                    .where(Order.customer_id == ctx.verified_customer_id)
                    .order_by(Order.order_date.desc())
                    .limit(50)
                )
            ).all()
        return json.dumps({"orders": [_order_view(o) for o in orders]})

    @tool
    async def get_order(order_id: str, config: RunnableConfig) -> str:
        """获取属于当前已登录客户的某笔订单详情。"""
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        async with db.SessionLocal() as session:
            order, err = await _get_owned_order(ctx, session, order_id)
        if err:
            return err
        return json.dumps(_order_view(order))

    # 资格查询只返回判断结果，不写退款记录；即使 eligible=True，也不代表已经退款。
    @tool
    async def check_refund_eligibility(order_id: str, config: RunnableConfig) -> str:
        """查询本人订单退款资格，不写入退款记录。"""
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        return json.dumps(await RefundService().check(ctx.verified_customer_id, order_id))

    @tool
    async def issue_refund(order_id: str, config: RunnableConfig) -> str:
        """复查退款规则并记录模拟退款结果。"""
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        return json.dumps(await RefundService().execute(
            ctx.verified_customer_id, order_id, ctx.conversation_id,
        ))

    @tool
    async def escalate_to_human(order_id: str, reason: str, config: RunnableConfig) -> str:
        """仅将满足条件且超出自动批准金额的退款申请转人工。"""
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        return json.dumps(await RefundService().execute(
            ctx.verified_customer_id, order_id, ctx.conversation_id, reason=reason,
        ))

    return [
        get_my_profile,
        list_orders,
        get_order,
        check_refund_eligibility,
        issue_refund,
        escalate_to_human,
    ]


# 工具对象按进程创建一次；每轮会话通过 tool_config 单独注入身份，避免串用客户。
TOOLS: list = _build_tools()
