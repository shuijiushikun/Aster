"""退款规则、锁和账本写入的共用入口，供 MCP 与历史脚本工具调用。"""

from sqlalchemy import select

from app.after_sales.locking import begin_order_write, lock_order
from app.after_sales.refunds import evaluate_refund, reject_active_on_refund
from app.after_sales.requests import remember, replay
from app.db import session as db
from app.db.models import Customer, Order, Refund

ESCALATION_REASON_PREFIX = "[智能体整理的客户诉求；内容未经核实] "


def not_found(order_id):
    return {"error": "order_not_found", "order_id": order_id}


class RefundService:
    async def check(self, customer_id: str, order_id: str) -> dict:
        async with db.SessionLocal() as session:
            order = await session.get(Order, order_id)
            if order is None or order.customer_id != customer_id:
                return not_found(order_id)
            customer = await session.get(Customer, customer_id)
            result = await evaluate_refund(session, order, customer)
            return {"order_id": order_id, **result.to_dict()}

    async def execute(self, customer_id: str, order_id: str, conversation_id: str,
                      *, request_id: str | None = None, reason: str | None = None) -> dict:
        reason = " ".join(reason.split())[:500] if reason is not None else None
        async with db.SessionLocal() as session, session.begin():
            await begin_order_write(session)
            order = await lock_order(session, order_id)
            if order is None or order.customer_id != customer_id:
                return not_found(order_id)
            return await self._execute_locked(session, order, conversation_id, request_id=request_id, reason=reason)

    async def _execute_locked(self, session, order, conversation_id, *, request_id, reason=None):
        """Caller holds Order lock; confirmation consumption shares this transaction."""
        operation = "issue_refund" if reason is None else "escalate_to_human"
        payload = {"order_id": order.id, "conversation_id": conversation_id, "reason": reason}
        recorded = await replay(session, order.customer_id, operation, request_id, payload)
        if recorded is not None:
            return recorded
        customer = await session.get(Customer, order.customer_id)
        result = await evaluate_refund(session, order, customer)
        if reason is not None:
            response = await self._escalate(session, order, conversation_id, reason, result)
        else:
            session.add(Refund(
                order_id=order.id, amount=order.amount, decision=result.decision,
                reason=" ".join(result.reasons), decided_by="agent", conversation_id=conversation_id,
            ))
            if result.decision == "approved":
                order.refunded = True
                await reject_active_on_refund(session, order.id)
            response = {"order_id": order.id, "decision": result.decision, "refund_recorded": True,
                        "amount": float(order.amount) if result.decision == "approved" else 0.0,
                        "reasons": list(result.reasons)}
        remember(session, order.customer_id, operation, request_id, payload, response)
        return response

    async def _escalate(self, session, order, conversation_id, reason, result):
        if not (result.eligible and result.requires_escalation):
            return {"error": "escalation_not_allowed", "order_id": order.id,
                    "decision": "denied", "refund_recorded": False,
                    "message": "该订单不符合转人工审核的条件。", "reasons": list(result.reasons)}
        existing = await session.scalar(select(Refund.decision).where(
            Refund.order_id == order.id, Refund.decision.in_(("escalated", "approved")),
        ).limit(1))
        if existing is None:
            session.add(Refund(
                order_id=order.id, amount=order.amount, decision="escalated",
                reason=ESCALATION_REASON_PREFIX + reason, decided_by="agent",
                conversation_id=conversation_id,
            ))
        return {
            "order_id": order.id, "decision": existing or "escalated",
            "already_escalated": existing == "escalated",
            "message": "该申请已转交人工审核，本次没有重复提交。" if existing == "escalated"
            else "该订单的退款此前已批准。" if existing == "approved" else "已转交人工专员审核。",
        }
