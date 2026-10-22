"""Runtime demo samples stay separate from legacy refund regression fixtures."""

import pytest
from sqlalchemy import func, select

from app.db import session as db
from app.db.models import AfterSalesTicket, ExchangeAcceptance, Order, Product, TicketApproval
from app.db.seed import seed_demo_samples, seed_if_empty


async def test_demo_seed_is_additive_repeatable_and_has_real_business_facts(engine):
    async with db.SessionLocal() as session:
        original = await session.get(Order, "ORD-1001")
        before = (original.product_name, original.delivered_date, original.refunded)
    assert not await seed_if_empty()
    assert await seed_demo_samples() == {"products": 2, "customers": 3, "orders": 13, "tickets": 5}
    assert await seed_demo_samples() == {"products": 0, "customers": 0, "orders": 0, "tickets": 0}
    async with db.SessionLocal() as session:
        original = await session.get(Order, "ORD-1001")
        assert (original.product_name, original.delivered_date, original.refunded) == before
        facts = list(await session.execute(select(Order, Product).join(Product).where(
            Order.id.like("ORD-DEMO-%"))))
        assert len(facts) == 13
        assert all(o.category == p.category and p.model_code in ("HP-01", "TSHIRT-01")
                   for o, p in facts)
        states = dict((await session.execute(select(AfterSalesTicket.order_id, AfterSalesTicket.status))).all())
        assert states == {
            "ORD-DEMO-3001": "WAITING_INFO", "ORD-DEMO-3002": "PENDING_REVIEW",
            "ORD-DEMO-3003": "CANCELLED", "ORD-DEMO-3004": "REJECTED", "ORD-DEMO-3005": "COMPLETED",
        }
        assert await session.scalar(select(func.count()).select_from(TicketApproval)) == 2
        assert await session.scalar(select(func.count()).select_from(ExchangeAcceptance)) == 1


async def test_interrupted_seed_resumes_its_approval_without_overwriting_user_cancel(engine, monkeypatch):
    from app.after_sales.schemas import RequestInput
    from app.after_sales.service import TicketService
    from app.auth import Principal

    original = TicketService.accept_exchange

    async def interrupted(*args, **kwargs):
        raise RuntimeError("stop after approval commit")

    monkeypatch.setattr(TicketService, "accept_exchange", interrupted)
    with pytest.raises(RuntimeError, match="approval commit"):
        await seed_demo_samples()
    monkeypatch.setattr(TicketService, "accept_exchange", original)
    assert await seed_demo_samples() == {"products": 0, "customers": 0, "orders": 0, "tickets": 0}
    async with db.SessionLocal() as session:
        pending = await session.scalar(select(AfterSalesTicket).where(
            AfterSalesTicket.order_id == "ORD-DEMO-3002"))
    await TicketService().cancel(Principal("CUST-DEMO-003", "customer"), pending.id,
                                 RequestInput(request_id="user-cancel"))
    await seed_demo_samples()
    async with db.SessionLocal() as session:
        assert (await session.get(AfterSalesTicket, pending.id)).status == "CANCELLED"
        assert await session.scalar(select(func.count()).select_from(AfterSalesTicket)) == 5
        assert await session.scalar(select(func.count()).select_from(ExchangeAcceptance)) == 1
