import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.after_sales.schemas import DecisionInput, ExchangeInput, RequestInput, SupplementInput
from app.after_sales.service import TicketError, TicketService
from app.auth import Principal
from app.db import session as db
from app.db.models import (
    AfterSalesTicket,
    Conversation,
    ExchangeAcceptance,
    Order,
    Refund,
    TicketApproval,
    TicketEvent,
    TicketRequest,
    utcnow,
)
from tests.conftest import admin_auth, auth
from tests.test_tools import _call, _ctx

ALICE = Principal("CUST-001", "customer")
BOB = Principal("CUST-002", "customer")
ADMIN = Principal("reviewer", "admin")
OTHER_SCOPE = Principal("reviewer", "admin", "CUST-002")
DATA = ExchangeInput(
    description="右耳无声，已重新配对", reason_type="defect", requested_change="更换同款"
)


async def create(**kwargs):
    return await TicketService().create_or_find_exchange(ALICE, "ORD-1001", DATA, **kwargs)


async def approve(ticket_id, request_id="approve-1"):
    return await TicketService().decide(
        ADMIN, ticket_id, DecisionInput(decision="approved", request_id=request_id)
    )


async def count(model):
    async with db.SessionLocal() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_create_in_two_conversations_returns_one_active_ticket(engine):
    async with db.SessionLocal() as session:
        session.add_all(
            [
                Conversation(id="conv-1", customer_id=ALICE.sub),
                Conversation(id="conv-2", customer_id=ALICE.sub),
            ]
        )
        await session.commit()
    one, two = await asyncio.gather(
        create(origin_conversation_id="conv-1"), create(origin_conversation_id="conv-2")
    )
    assert one.id == two.id
    assert one.status == "PENDING_REVIEW"
    assert one.workflow_thread_id == "exchange:" + one.id
    assert await count(AfterSalesTicket) == 1
    assert await count(TicketEvent) == 1


async def test_active_ticket_has_a_database_unique_backstop(engine):
    one = await create()
    async with db.SessionLocal() as session:
        session.add(
            AfterSalesTicket(
                id="another",
                customer_id=ALICE.sub,
                order_id=one.order_id,
                workflow_thread_id="exchange:another",
                description="duplicate",
                status="OPEN",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_ownership_is_checked_before_lookup_or_replay(engine):
    service = TicketService()
    with pytest.raises(TicketError) as error:
        await service.create_or_find_exchange(BOB, "ORD-1001", DATA)
    assert error.value.status_code == 404
    ticket = await create()
    for actor in (BOB, OTHER_SCOPE):
        with pytest.raises(TicketError) as error:
            await service.detail(actor, ticket.id)
        assert error.value.status_code == 404
    with pytest.raises(TicketError):
        await service.create_or_find_exchange(
            ALICE, "ORD-1001", DATA, origin_conversation_id="missing"
        )


async def test_original_submission_date_survives_waiting_and_approval(engine, monkeypatch):
    ticket = await create()
    from app.after_sales import service as module

    monkeypatch.setattr(module, "utcnow", lambda: utcnow() + timedelta(days=40))
    assert (await create()).id == ticket.id
    assert (await approve(ticket.id))["status"] == "APPROVED"
    first, second = await asyncio.gather(
        TicketService().accept_exchange(ticket.id), TicketService().accept_exchange(ticket.id)
    )
    assert first.id == second.id
    assert first.reference_code == second.reference_code
    assert await count(ExchangeAcceptance) == 1
    assert (await create()).id == ticket.id
    assert (await TicketService().detail(ALICE, ticket.id)).status == "COMPLETED"


async def test_cancellation_and_reapplication_use_new_submission_time(engine, monkeypatch):
    ticket = await create()
    service = TicketService()
    cancelled = await service.cancel(ALICE, ticket.id, RequestInput(request_id="cancel-1"))
    assert cancelled["status"] == "CANCELLED"
    assert await service.cancel(ALICE, ticket.id, RequestInput(request_id="cancel-2")) != cancelled
    assert await count(TicketEvent) == 2  # 新取消请求不再写业务事件。
    from app.after_sales import service as module

    monkeypatch.setattr(module, "utcnow", lambda: utcnow() + timedelta(days=40))
    new = await create()
    assert new.id != ticket.id
    assert new.status == "REJECTED" and new.last_error_code == "EXCHANGE_WINDOW_EXPIRED"


async def test_missing_fields_and_durable_request_replay(engine):
    service = TicketService()
    ticket = await service.create_or_find_exchange(
        ALICE, "ORD-1001", ExchangeInput(description="想换货")
    )
    assert ticket.status == "WAITING_INFO"
    supplement = SupplementInput(
        description="右耳无声",
        reason_type="defect",
        requested_change="更换同款",
        request_id="info-1",
    )
    response = await service.supplement(ALICE, ticket.id, supplement)
    assert response["status"] == "PENDING_REVIEW"
    await approve(ticket.id)
    assert await service.supplement(ALICE, ticket.id, supplement) == response
    with pytest.raises(TicketError) as error:
        await service.supplement(
            ALICE, ticket.id, supplement.model_copy(update={"description": "不同材料"})
        )
    assert error.value.status_code == 409
    with pytest.raises(TicketError) as error:
        await service.supplement(BOB, ticket.id, supplement)
    assert error.value.status_code == 404
    assert await count(TicketRequest) == 2
    assert await count(TicketEvent) == 3


async def test_same_decision_with_new_id_keeps_original_approval(engine):
    ticket = await create()
    first = await approve(ticket.id)
    await TicketService().accept_exchange(ticket.id)
    assert await approve(ticket.id) == first
    again = await TicketService().decide(
        ADMIN,
        ticket.id,
        DecisionInput(decision="approved", request_id="approve-2", comment="different comment"),
    )
    assert again["status"] == "COMPLETED"
    assert await count(TicketApproval) == 1
    with pytest.raises(TicketError) as error:
        await TicketService().decide(
            ADMIN, ticket.id, DecisionInput(decision="rejected", request_id="reject")
        )
    assert error.value.status_code == 409
    async with db.SessionLocal() as session:
        approval = await session.scalar(select(TicketApproval))
    assert approval.comment == ""


async def test_cancellation_and_approval_race_has_only_one_winner(engine):
    ticket = await create()
    results = await asyncio.gather(
        approve(ticket.id),
        TicketService().cancel(ALICE, ticket.id, RequestInput(request_id="cancel")),
        return_exceptions=True,
    )
    assert sum(isinstance(r, TicketError) and r.status_code == 409 for r in results) == 1
    assert sum(isinstance(r, dict) for r in results) == 1


@pytest.mark.parametrize("approved", [False, True])
async def test_refund_rejects_active_ticket_and_prevents_later_acceptance(engine, approved):
    ticket = await create()
    if approved:
        await approve(ticket.id)
    result = await _call(_ctx(ALICE.sub), "issue_refund", order_id=ticket.order_id)
    assert result["decision"] == "approved"
    detail = await TicketService().detail(ALICE, ticket.id)
    assert detail.status == "REJECTED" and detail.last_error_code == "ORDER_REFUNDED"
    assert detail.events[-1].kind == "ORDER_REFUNDED"
    with pytest.raises(TicketError):
        await TicketService().accept_exchange(ticket.id)
    assert await count(ExchangeAcceptance) == 0


async def test_completed_exchange_blocks_all_refund_entrypoints(engine):
    ticket = await create()
    await approve(ticket.id)
    await TicketService().accept_exchange(ticket.id)
    for name in ("check_refund_eligibility", "issue_refund", "escalate_to_human"):
        args = {"reason": "refund after exchange"} if name == "escalate_to_human" else {}
        result = await _call(_ctx(ALICE.sub), name, order_id=ticket.order_id, **args)
        assert result.get("decision") == "denied" or result.get("error") == "escalation_not_allowed"
    async with db.SessionLocal() as session:
        assert (await session.get(Order, ticket.order_id)).refunded is False


async def test_refund_and_acceptance_race_are_mutually_exclusive(engine):
    ticket = await create()
    await approve(ticket.id)
    await asyncio.gather(
        TicketService().accept_exchange(ticket.id),
        _call(_ctx(ALICE.sub), "issue_refund", order_id=ticket.order_id),
        return_exceptions=True,
    )
    async with db.SessionLocal() as session:
        accepted = await session.scalar(select(func.count()).select_from(ExchangeAcceptance))
        refunded = await session.scalar(
            select(func.count()).select_from(Refund).where(Refund.decision == "approved")
        )
    assert accepted + refunded == 1


async def test_terminal_failure_cannot_be_bypassed_by_reapplication(engine):
    ticket = await create()
    async with db.SessionLocal() as session:
        stored = await session.get(AfterSalesTicket, ticket.id)
        stored.status = "FAILED_TERMINAL"
        await session.commit()
    with pytest.raises(TicketError) as error:
        await create()
    assert error.value.code == "MANUAL_REQUIRED"


async def test_get_api_enforces_scope_and_exposes_no_write_endpoints(client):
    ticket = await create()
    service = TicketService()
    mine = await client.get("/api/me/tickets", headers=auth(ALICE.sub))
    assert mine.status_code == 200 and mine.json()[0]["id"] == ticket.id
    assert (
        await client.get("/api/me/tickets/" + ticket.id, headers=auth(BOB.sub))
    ).status_code == 404
    assert (await client.get("/api/me/tickets/unknown", headers=auth(BOB.sub))).status_code == 404
    assert (await client.get("/api/admin/tickets", headers=auth(ALICE.sub))).status_code == 403
    detail = await client.get("/api/admin/tickets/" + ticket.id, headers=admin_auth())
    assert detail.status_code == 200 and detail.json()["order"]["id"] == ticket.order_id
    assert (
        await client.get("/api/admin/tickets?status=INVALID", headers=admin_auth())
    ).status_code == 422
    assert (
        await client.get("/api/me/tickets?limit=1000", headers=auth(ALICE.sub))
    ).status_code == 422
    assert await service.list_tickets(OTHER_SCOPE) == []
    assert (await client.post("/api/me/tickets", headers=auth(ALICE.sub), json={})).status_code in (
        404,
        405,
    )
    assert (
        await client.post(
            "/api/admin/tickets/" + ticket.id + "/decision", headers=admin_auth(), json={}
        )
    ).status_code in (404, 405)
