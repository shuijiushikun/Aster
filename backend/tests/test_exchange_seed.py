from sqlalchemy import func, select

from app.db import session as db
from app.db.models import Order, Product
from tests.seed_data import seed_exchange_samples


async def test_exchange_samples_are_additive_and_repeatable(engine):
    async with db.SessionLocal() as session:
        before = await session.scalar(select(func.count()).select_from(Order))
        old = await session.get(Order, "ORD-1001")
        original = (old.product_name, old.delivered_date, old.refunded)
    assert await seed_exchange_samples() == {"products": 2, "customers": 2, "orders": 4}
    assert await seed_exchange_samples() == {"products": 0, "customers": 0, "orders": 0}
    async with db.SessionLocal() as session:
        old = await session.get(Order, "ORD-1001")
        assert (old.product_name, old.delivered_date, old.refunded) == original
        assert old.product_id is None
        assert await session.scalar(select(func.count()).select_from(Order)) == before + 4
        assert await session.scalar(select(func.count()).select_from(Product)) == 2
        assert (await session.get(Order, "ORD-EX-1001")).product_id == "PRD-HEADPHONE-01"
