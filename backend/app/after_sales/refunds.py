"""原退款的唯一增量门：已受理换货不可退款，成功退款终止活跃换货。"""

from sqlalchemy import select

from app.db.models import (
    ACTIVE_TICKET_STATUSES,
    AfterSalesTicket,
    ExchangeAcceptance,
    TicketEvent,
    utcnow,
)
from app.policy.engine import PolicyResult, evaluate


async def evaluate_refund(session, order, customer):
    acceptance = await session.scalar(
        select(ExchangeAcceptance.id).where(ExchangeAcceptance.order_id == order.id)
    )
    if acceptance is not None:
        return PolicyResult(
            False, False, [f"订单 {order.id} 已成功受理换货，不能再退款。"], float(order.amount)
        )
    return evaluate(order, customer)


async def reject_active_on_refund(session, order_id):
    # 调用方已持有订单锁；统一保持Order→Ticket，不在此提交独立事务。
    tickets = await session.scalars(
        select(AfterSalesTicket)
        .where(
            AfterSalesTicket.order_id == order_id,
            AfterSalesTicket.status.in_(ACTIVE_TICKET_STATUSES),
        )
        .order_by(AfterSalesTicket.id)
        .with_for_update()
    )
    for ticket in tickets:
        ticket.status = "REJECTED"
        ticket.version += 1
        ticket.updated_at = utcnow()
        ticket.last_error_code = "ORDER_REFUNDED"
        ticket.last_error_message = None
        session.add(
            TicketEvent(
                ticket_id=ticket.id,
                kind="ORDER_REFUNDED",
                actor_type="system",
                actor_id="refund",
                payload_json={"status": "REJECTED", "code": "ORDER_REFUNDED"},
            )
        )
