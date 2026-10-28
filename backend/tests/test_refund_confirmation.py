"""A model may prepare a refund; only an authenticated button action executes it."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.agent.runner import create_conversation
from app.db import session as db
from app.db.models import Message, Order, Refund, utcnow


async def prepare(customer="CUST-001", order="ORD-1001"):
    from app.after_sales.refund_confirmation import RefundConfirmationService

    cid = await create_conversation(customer)
    async with db.SessionLocal() as session:
        msg = Message(conversation_id=cid, role="user", content=f"请退款 {order}")
        session.add(msg)
        await session.commit()
        mid = msg.id
    result = await RefundConfirmationService().prepare(customer, order, cid, message_id=mid,
                                                      request_id=f"request-{mid}")
    return cid, mid, result


async def refund_count():
    async with db.SessionLocal() as session:
        return await session.scalar(select(func.count()).select_from(Refund))


async def test_preparation_has_database_snapshot_and_never_refunds(engine):
    from app.after_sales.refund_confirmation import RefundConfirmationService

    cid, mid, result = await prepare()
    assert result["decision"] == "awaiting_confirmation"
    proposal = result["confirmation"]
    assert proposal["order_id"] == "ORD-1001" and proposal["currency"] == "USD"
    assert proposal["product_name"] and "model_code" in proposal
    assert await refund_count() == 0
    again = await RefundConfirmationService().prepare("CUST-001", "ORD-1001", cid,
                         message_id=mid, request_id=f"request-{mid}")
    assert again["confirmation"]["id"] == proposal["id"]


async def test_confirm_is_idempotent_and_saves_user_action_and_business_reply(engine):
    from app.after_sales.refund_confirmation import RefundConfirmationService

    service = RefundConfirmationService()
    cid, _, proposal = await prepare()
    key = proposal["confirmation"]["id"]
    first = await service.decide("CUST-001", cid, key, "confirm")
    assert first["result"]["decision"] == "approved"
    assert await service.decide("CUST-001", cid, key, "confirm") == first
    assert await refund_count() == 1
    async with db.SessionLocal() as session:
        assert (await session.get(Order, "ORD-1001")).refunded
        messages = list(await session.scalars(select(Message).where(Message.conversation_id == cid)))
        assert len(messages) == 3
        assert messages[-2].role == "user" and "确认" in messages[-2].content
        assert "模拟退款" in messages[-1].content


async def test_cancel_then_confirm_cannot_execute(engine):
    from app.after_sales.refund_confirmation import ConfirmationError, RefundConfirmationService

    service = RefundConfirmationService()
    cid, _, proposal = await prepare()
    key = proposal["confirmation"]["id"]
    assert (await service.decide("CUST-001", cid, key, "cancel"))["status"] == "cancelled"
    with pytest.raises(ConfirmationError):
        await service.decide("CUST-001", cid, key, "confirm")
    assert await refund_count() == 0


@pytest.mark.parametrize("change", ["amount", "model", "new_message", "expiry"])
async def test_changed_or_expired_proposal_cannot_execute(engine, change):
    from app.after_sales.refund_confirmation import ConfirmationError, RefundConfirmationService
    from app.db.models import RefundConfirmation

    cid, _, proposal = await prepare()
    key = proposal["confirmation"]["id"]
    async with db.SessionLocal() as session:
        order = await session.get(Order, "ORD-1001")
        if change == "amount":
            order.amount += 1
        elif change == "model":
            order.product_name = "另一款商品"
        elif change == "new_message":
            session.add(Message(conversation_id=cid, role="user", content="算了，先别退"))
        else:
            (await session.get(RefundConfirmation, key)).expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(ConfirmationError) as failure:
        await RefundConfirmationService().decide("CUST-001", cid, key, "confirm")
    assert failure.value.status_code == 409
    assert await refund_count() == 0


async def test_confirmation_is_bound_to_customer_and_conversation(engine):
    from app.after_sales.refund_confirmation import ConfirmationError, RefundConfirmationService

    cid, _, proposal = await prepare()
    other_cid = await create_conversation("CUST-001")
    for owner, conversation in [("CUST-002", cid), ("CUST-001", other_cid)]:
        with pytest.raises(ConfirmationError) as failure:
            await RefundConfirmationService().decide(owner, conversation, proposal["confirmation"]["id"], "confirm")
        assert failure.value.status_code == 404
    assert await refund_count() == 0


async def test_conversational_yes_does_not_execute_and_invalidates_old_proposal(engine):
    from app.after_sales.refund_confirmation import RefundConfirmationService

    cid, _, _ = await prepare()
    async with db.SessionLocal() as session:
        session.add(Message(conversation_id=cid, role="user", content="好的"))
        await session.commit()
    assert await RefundConfirmationService().pending("CUST-001", cid) is None
    assert await refund_count() == 0


async def test_confirmation_api_rejects_foreign_identity_and_extra_amount(client):
    from app.auth import mint_token

    cid, _, proposal = await prepare()
    path = f"/api/me/conversations/{cid}/refund-confirmations/{proposal['confirmation']['id']}"
    foreign = await client.post(path, json={"decision": "confirm"},
                               headers={"Authorization": f"Bearer {mint_token('CUST-002', 'customer')}"})
    assert foreign.status_code == 404
    own = {"Authorization": f"Bearer {mint_token('CUST-001', 'customer')}"}
    forged = await client.post(path, json={"decision": "confirm", "amount": 0.01}, headers=own)
    assert forged.status_code == 422
    assert await refund_count() == 0
    response = await client.post(path, json={"decision": "confirm"}, headers=own)
    assert response.status_code == 200 and response.json()["result"]["decision"] == "approved"


async def test_declined_order_does_not_open_a_confirmation(engine):
    _, _, result = await prepare("CUST-002", "ORD-1002")
    assert result["decision"] == "denied"
    assert not result.get("confirmation")
    assert await refund_count() == 0


async def test_parallel_confirmation_has_one_refund_and_one_audit_action(engine):
    import asyncio

    from app.after_sales.refund_confirmation import RefundConfirmationService

    cid, _, proposal = await prepare()
    key = proposal["confirmation"]["id"]
    service = RefundConfirmationService()
    results = await asyncio.gather(*(service.decide("CUST-001", cid, key, "confirm") for _ in range(4)))
    assert all(result == results[0] for result in results)
    assert await refund_count() == 1
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Message)
                                    .where(Message.conversation_id == cid)) == 3


async def test_refund_failure_rolls_back_confirmation_consumption(engine, monkeypatch):
    from app.after_sales.refund_confirmation import RefundConfirmationService
    from app.db.models import RefundConfirmation

    cid, _, proposal = await prepare()
    key = proposal["confirmation"]["id"]

    async def failed(*args, **kwargs):
        raise RuntimeError("database failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(RefundConfirmationService, "_record_action", failed)
        with pytest.raises(RuntimeError):
            await RefundConfirmationService().decide("CUST-001", cid, key, "confirm")
    async with db.SessionLocal() as session:
        assert (await session.get(RefundConfirmation, key)).status == "pending"
        assert not (await session.get(Order, "ORD-1001")).refunded
    assert await refund_count() == 0
    assert (await RefundConfirmationService().decide("CUST-001", cid, key, "confirm"))["result"]["decision"] == "approved"


async def test_qualification_is_rechecked_after_customer_confirms(engine):
    from app.after_sales.refund_confirmation import RefundConfirmationService

    cid, _, proposal = await prepare()
    async with db.SessionLocal() as session, session.begin():
        (await session.get(Order, "ORD-1001")).is_final_sale = True
    response = await RefundConfirmationService().decide("CUST-001", cid, proposal["confirmation"]["id"], "confirm")
    assert response["result"]["decision"] == "denied"
    async with db.SessionLocal() as session:
        assert not (await session.get(Order, "ORD-1001")).refunded


async def test_confirmation_api_uses_same_conversation_lock_as_chat(client):
    from app import redis as shared
    from app.auth import mint_token

    cid, _, proposal = await prepare()
    lock = await shared.acquire_turn_lock(cid, 30)
    try:
        response = await client.post(f"/api/me/conversations/{cid}/refund-confirmations/{proposal['confirmation']['id']}",
            json={"decision": "confirm"}, headers={"Authorization": f"Bearer {mint_token('CUST-001', 'customer')}"})
        assert response.status_code == 409
        assert await refund_count() == 0
    finally:
        await shared.release_turn_lock(cid, lock)


async def test_manual_review_recheck_returns_a_structured_denial(client):
    from app.auth import mint_token

    async with db.SessionLocal() as session, session.begin():
        (await session.get(Order, "ORD-1001")).amount = 999
    cid, _, proposal = await prepare()
    assert proposal["confirmation"]["operation"] == "escalate_to_human"
    async with db.SessionLocal() as session, session.begin():
        (await session.get(Order, "ORD-1001")).is_final_sale = True
    url = f"/api/me/conversations/{cid}/refund-confirmations/{proposal['confirmation']['id']}"
    headers = {"Authorization": f"Bearer {mint_token('CUST-001', 'customer')}"}
    response = await client.post(url, json={"decision": "confirm"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["result"]["decision"] == "denied"
    assert not response.json()["result"]["refund_recorded"]
    assert await refund_count() == 0
    replay = await client.post(url, json={"decision": "confirm"}, headers=headers)
    assert replay.json() == response.json()


@pytest.mark.parametrize("amount,threshold", [(129.99, 100), (999, 2000)])
async def test_changed_refund_operation_requires_fresh_consent(engine, monkeypatch, amount, threshold):
    from app.after_sales.refund_confirmation import ConfirmationError, RefundConfirmationService
    from app.config import get_settings

    async with db.SessionLocal() as session, session.begin():
        (await session.get(Order, "ORD-1001")).amount = amount
    cid, _, proposal = await prepare()
    monkeypatch.setattr(get_settings(), "escalation_threshold", threshold)
    with pytest.raises(ConfirmationError) as failure:
        await RefundConfirmationService().decide("CUST-001", cid, proposal["confirmation"]["id"], "confirm")
    assert failure.value.status_code == 409
    assert await refund_count() == 0
