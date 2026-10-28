"""Persist the exact refund a customer may approve outside the model/tool loop."""

from datetime import timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update

from app.after_sales.locking import begin_order_write, lock_order
from app.after_sales.refund_service import RefundService
from app.after_sales.refunds import evaluate_refund
from app.after_sales.requests import payload_hash
from app.db import session as db
from app.db.models import (
    Conversation,
    Customer,
    Message,
    Product,
    ReasoningEvent,
    RefundConfirmation,
    utcnow,
)
from app.db.turns import lock_conversation
from app.localization import product_name


class ConfirmationError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code, self.message = status_code, message
        super().__init__(message)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def _owned_conversation(session, customer_id, cid):
    conversation = await session.get(Conversation, cid)
    if conversation is None or conversation.customer_id != customer_id:
        raise ConfirmationError(404, "未找到可访问的会话或退款确认单。")


async def _latest_message_id(session, cid):
    return await session.scalar(select(Message.id).where(
        Message.conversation_id == cid, Message.role == "user").order_by(Message.id.desc()).limit(1))


async def _snapshot(session, order):
    product = await session.get(Product, order.product_id) if order.product_id else None
    return {"order_id": order.id, "product_id": order.product_id,
            "product_name": product_name(order.product_name),
            "model_code": product.model_code if product else None,
            "amount": str(order.amount), "currency": "USD", "order_status": order.status,
            "already_refunded": order.refunded}


def view(row: RefundConfirmation) -> dict:
    return {**{key: row.snapshot_json[key] for key in
               ("order_id", "product_name", "model_code", "amount", "currency")},
            "id": row.id, "conversation_id": row.conversation_id, "operation": row.operation,
            "status": row.status, "expires_at": _aware(row.expires_at).isoformat()}


def confirmation_reply(proposal: dict) -> str:
    action = "提交人工退款审核" if proposal["operation"] == "escalate_to_human" else "办理模拟退款"
    return (f"已核对订单 **{proposal['order_id']}**，商品为 **{proposal['product_name']}**，"
            f"申请金额 **{proposal['amount']} {proposal['currency']}**。"
            f"请在确认框中核对后点击确认，再{action}。现在尚未执行，选择取消将保留原订单状态。")


class RefundConfirmationService:
    async def prepare(self, customer_id: str, order_id: str, cid: str, *,
                      message_id: int | None, request_id: str, reason: str | None = None) -> dict:
        reason = " ".join(reason.split())[:500] if reason is not None else None
        digest = payload_hash({"order_id": order_id, "cid": cid, "message_id": message_id, "reason": reason})
        async with db.SessionLocal() as session, session.begin():
            await begin_order_write(session)
            await _owned_conversation(session, customer_id, cid)
            order = await lock_order(session, order_id)
            if order is None or order.customer_id != customer_id:
                raise ConfirmationError(404, "未找到可访问的订单。")
            if message_id is None or await _latest_message_id(session, cid) != message_id:
                raise ConfirmationError(409, "本轮请求已变化，请重新提出退款申请。")
            old = await session.scalar(select(RefundConfirmation).where(
                RefundConfirmation.customer_id == customer_id, RefundConfirmation.request_id == request_id))
            if old:
                if old.payload_hash != digest:
                    raise ConfirmationError(409, "同一请求的退款内容发生变化，请重新申请。")
                if old.status == "confirmed":
                    return old.result_json
                if old.status != "pending" or _aware(old.expires_at) <= utcnow():
                    raise ConfirmationError(409, "本次确认已失效，请重新申请退款。")
                return {"order_id": order_id, "decision": "awaiting_confirmation", "confirmation": view(old)}
            customer = await session.get(Customer, customer_id)
            eligible = await evaluate_refund(session, order, customer)
            if reason is not None and not (eligible.eligible and eligible.requires_escalation):
                return {"error": "escalation_not_allowed", "order_id": order_id,
                        "message": "该订单不符合转人工审核的条件。", "reasons": list(eligible.reasons)}
            if not eligible.eligible:
                return {"order_id": order_id, "decision": "denied", "reasons": list(eligible.reasons)}
            operation = "escalate_to_human" if eligible.requires_escalation else "issue_refund"
            if operation == "escalate_to_human":
                reason = reason or "客户申请退款，订单金额超过自动批准范围。"
            else:
                reason = None
            row = RefundConfirmation(id=uuid4().hex, customer_id=customer_id, conversation_id=cid,
                message_id=message_id, order_id=order_id, request_id=request_id, payload_hash=digest,
                operation=operation, reason=reason, snapshot_json=await _snapshot(session, order),
                status="pending", expires_at=utcnow() + timedelta(minutes=10))
            session.add(row)
            await session.flush()
            return {"order_id": order_id, "decision": "awaiting_confirmation", "confirmation": view(row)}

    async def pending(self, customer_id: str, cid: str) -> dict | None:
        async with db.SessionLocal() as session:
            await _owned_conversation(session, customer_id, cid)
            mid = await _latest_message_id(session, cid)
            row = await session.scalar(select(RefundConfirmation).where(
                RefundConfirmation.customer_id == customer_id, RefundConfirmation.conversation_id == cid,
                RefundConfirmation.message_id == mid, RefundConfirmation.status == "pending",
                RefundConfirmation.expires_at > utcnow()).order_by(RefundConfirmation.created_at.desc()).limit(1))
            return view(row) if row else None

    async def decide(self, customer_id: str, cid: str, confirmation_id: str, decision: str, *,
                     lock_token: str | None = None) -> dict:
        if decision not in {"confirm", "cancel"}:
            raise ConfirmationError(422, "请选择确认或取消。")
        failure = None
        async with db.SessionLocal() as session, session.begin():
            await _owned_conversation(session, customer_id, cid)
            await lock_conversation(session, cid, lock_token)
            # 会话版本串行后，业务写入继续保持 Order -> Confirmation -> Ticket。
            ref = await session.scalar(select(RefundConfirmation).where(
                RefundConfirmation.id == confirmation_id, RefundConfirmation.customer_id == customer_id,
                RefundConfirmation.conversation_id == cid))
            if ref is None:
                raise ConfirmationError(404, "未找到可访问的退款确认单。")
            order = await lock_order(session, ref.order_id)
            row = await session.scalar(select(RefundConfirmation).where(RefundConfirmation.id == ref.id)
                                       .with_for_update().execution_options(populate_existing=True))
            if order is None or order.customer_id != customer_id:
                raise ConfirmationError(404, "未找到可访问的订单。")
            if row.status != "pending":
                if (row.status, decision) in {("confirmed", "confirm"), ("cancelled", "cancel")}:
                    return {"id": row.id, "status": row.status, "result": row.result_json}
                raise ConfirmationError(409, "本次确认已处理或失效，请重新查询订单状态。")
            if decision == "confirm":
                if _aware(row.expires_at) <= utcnow():
                    row.status, failure = "expired", "确认已超时，请重新申请退款并核对信息。"
                elif await _latest_message_id(session, cid) != row.message_id:
                    row.status, failure = "stale", "您已发送新的消息，请重新核对本次退款申请。"
                elif await _snapshot(session, order) != row.snapshot_json:
                    row.status, failure = "stale", "订单信息已变化，请重新申请并核对商品与金额。"
                if failure is None:
                    eligibility = await evaluate_refund(session, order, await session.get(Customer, customer_id))
                    operation = "escalate_to_human" if eligibility.requires_escalation else "issue_refund"
                    if eligibility.eligible and operation != row.operation:
                        row.status, failure = "stale", "退款处理方式已变化，请重新申请并核对是否需要人工审核。"
            if failure is None:
                result = None
                row.status = "cancelled" if decision == "cancel" else "confirmed"
                if decision == "confirm":
                    result = await RefundService()._execute_locked(session, order, cid,
                        request_id=f"confirmation:{row.id}", reason=row.reason)
                    row.result_json = result
                row.decided_at = utcnow()
                await self._record_action(session, row, result)
            response = {"id": row.id, "status": row.status, "result": row.result_json}
        # Commit invalidation before reporting it; never consume consent separately from the refund.
        if failure:
            raise ConfirmationError(409, failure)
        return response

    async def _record_action(self, session, row, result):
        action = "取消退款申请" if row.status == "cancelled" else "确认提交退款申请"
        user = Message(conversation_id=row.conversation_id, role="user",
                       content=f"{action}：{row.order_id}，{row.snapshot_json['amount']} USD。")
        session.add(user)
        await session.flush()
        if row.status == "cancelled":
            text = f"已取消订单 **{row.order_id}** 的本次退款申请，订单未发生退款。"
        elif result.get("decision") == "approved":
            text = f"订单 **{row.order_id}** 的模拟退款已记录，金额 **{result['amount']:.2f} USD**。本系统没有真实资金划转。"
        elif result.get("decision") == "escalated":
            text = f"订单 **{row.order_id}** 的退款申请已提交人工审核，尚未批准退款。请保留订单号，联系人工客服核实进度。"
        else:
            text = (f"订单 **{row.order_id}** 经复核暂时无法办理退款："
                    + ("；".join(result.get("reasons", [])) or result.get("message", "请查询订单当前状态。")))
        session.add(Message(conversation_id=row.conversation_id, role="assistant", content=text))
        seq = await session.scalar(update(Conversation).where(Conversation.id == row.conversation_id)
            .values(event_seq=Conversation.event_seq + 1).returning(Conversation.event_seq))
        session.add(ReasoningEvent(conversation_id=row.conversation_id, message_id=user.id, seq=seq,
            step_type="decision" if result else "notice", node="customer_confirmation",
            payload={"tool": row.operation, "confirmation_id": row.id, "customer_action": row.status,
                     "result": {"ok": True, "data": result} if result else None}, created_at=utcnow()))
