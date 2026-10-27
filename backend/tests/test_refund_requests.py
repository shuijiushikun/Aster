import pytest
from sqlalchemy import func, select

from app.after_sales.refund_service import RefundService
from app.after_sales.requests import RequestConflict
from app.db import session as db
from app.db.models import Refund


async def test_same_request_replays_committed_refund_before_rechecking_state(engine):
    service = RefundService()
    first = await service.execute("CUST-001", "ORD-1001", "conv", request_id="one")
    replay = await service.execute("CUST-001", "ORD-1001", "conv", request_id="one")
    assert first == replay
    assert first["decision"] == "approved"
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1
    other_request = await service.execute("CUST-001", "ORD-1001", "conv", request_id="two")
    assert other_request["decision"] == "denied"


async def test_request_replay_still_requires_ownership(engine):
    service = RefundService()
    await service.execute("CUST-001", "ORD-1001", "conv", request_id="one")
    result = await service.execute("CUST-003", "ORD-1001", "conv", request_id="one")
    assert result == {"error": "order_not_found", "order_id": "ORD-1001"}


async def test_same_request_id_cannot_be_reused_for_another_payload(engine):
    service = RefundService()
    await service.execute("CUST-001", "ORD-1001", "conv", request_id="one")
    with pytest.raises(RequestConflict):
        await service.execute("CUST-001", "ORD-1001", "another-conv", request_id="one")
