from sqlalchemy import func, select

from app.db import session as db
from app.db.models import Customer, Order
from app.db.seed import seed_demo_samples
from scripts.cleanup_legacy_demo import cleanup


async def test_cleanup_removes_exact_old_samples_and_keeps_new_demo(engine, tmp_path):
    await seed_demo_samples()
    preview = await cleanup()
    assert preview["customers"] == 15
    async with db.SessionLocal() as session:
        assert await session.get(Customer, "CUST-001")
    applied = await cleanup(apply=True, backup_dir=tmp_path)
    assert applied["customers"] == 15 and list(tmp_path.glob("legacy-demo-*.json"))
    assert (await cleanup())["customers"] == 0
    async with db.SessionLocal() as session:
        assert await session.get(Customer, "CUST-001") is None
        assert await session.scalar(select(func.count()).select_from(Customer)) == 3
        assert await session.scalar(select(func.count()).select_from(Order)) == 13
