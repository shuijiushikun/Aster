"""T5 recovery evidence: real PostgreSQL Saver, rebuilt pools and graphs."""

import asyncio
import os
import sys
from contextlib import AsyncExitStack

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.after_sales.schemas import ExchangeInput, RequestInput, SupplementInput
from app.after_sales.service import TicketService
from app.auth import ROLE_ADMIN, mint_token
from app.config import get_settings
from app.db import session as db
from app.db.models import ExchangeAcceptance
from app.workflow import graph, resume
from app.workflow.runtime import open_workflow
from tests import test_chat_graph as chat
from tests.conftest import admin_auth, auth
from tests.test_chat_graph import setup_chat  # noqa: F401 -- pytest fixture registration
from tests.test_tickets import ALICE, approve, count, create
from tests.test_tools import _call, _ctx

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="Requires a disposable TEST_DATABASE_URL (real Postgres checkpoint)",
)


@pytest.fixture
def event_loop_policy():
    # psycopg async requires SelectorEventLoop on Windows (Python 3.13).
    return (
        asyncio.WindowsSelectorEventLoopPolicy()
        if sys.platform == "win32"
        else asyncio.DefaultEventLoopPolicy()
    )


@pytest.fixture
async def restart(engine, monkeypatch):
    stack = AsyncExitStack()
    monkeypatch.setattr(resume, "get_runtime", lambda: None)

    async def rebuild():
        await stack.aclose()
        settings = get_settings().model_copy(
            update={"database_url": os.environ["TEST_DATABASE_URL"]}
        )
        runtime = await stack.enter_async_context(open_workflow(settings))
        monkeypatch.setattr(resume, "get_runtime", lambda: runtime)
        return runtime

    yield rebuild
    await stack.aclose()


async def snapshot(runtime, ticket_id):
    return await runtime.graph.aget_state({"configurable": {"thread_id": "exchange:" + ticket_id}})


async def status(ticket_id):
    return (await TicketService().detail(ALICE, ticket_id)).status


async def test_lost_start_and_lost_approval_wakeup_survive_rebuild(client, restart):
    ticket = await create(start_open=True)
    assert await status(ticket.id) == "OPEN"
    first = await restart()
    response = await client.post(
        f"/api/admin/tickets/{ticket.id}/resume",
        headers=admin_auth(),
        json={"request_id": "start"},
    )
    assert response.status_code == 202 and response.json()["status"] == "OPEN"
    state = await snapshot(first, ticket.id)
    assert state.tasks[0].interrupts[0].value["kind"] == "approval"
    assert await status(ticket.id) == "PENDING_REVIEW"
    await approve(ticket.id)  # commit without waking the graph
    second = await restart()
    assert first.graph is not second.graph and first.saver is not second.saver
    assert (await snapshot(second, ticket.id)).tasks[0].interrupts
    assert (await resume.coordinate(ticket.id))["status"] == "COMPLETED"
    assert not (await snapshot(second, ticket.id)).next
    assert await count(ExchangeAcceptance) == 1


async def test_information_interrupt_uses_committed_materials_after_restart(restart):
    ticket = await TicketService().create_or_find_exchange(
        ALICE,
        "ORD-1001",
        ExchangeInput(description="想换货"),
        start_open=True,
    )
    await restart()
    assert (await resume.coordinate(ticket.id))["status"] == "WAITING_INFO"
    runtime = await restart()
    assert (await snapshot(runtime, ticket.id)).tasks[0].interrupts[0].value[
        "kind"
    ] == "information"
    assert (await resume.coordinate(ticket.id))["state"] == "interrupted"
    assert await count(ExchangeAcceptance) == 0
    # A partial supplement must produce a new information interrupt, not execution.
    await TicketService().supplement(
        ALICE,
        ticket.id,
        SupplementInput(description="右耳无声", request_id="partial"),
    )
    assert (await resume.coordinate(ticket.id))["status"] == "WAITING_INFO"
    await TicketService().supplement(
        ALICE,
        ticket.id,
        SupplementInput(
            description="右耳无声",
            reason_type="defect",
            requested_change="更换同款",
            request_id="complete",
        ),
    )
    runtime = await restart()
    assert (await resume.coordinate(ticket.id))["status"] == "PENDING_REVIEW"
    assert (await snapshot(runtime, ticket.id)).tasks[0].interrupts[0].value["kind"] == "approval"
    await approve(ticket.id)
    assert (await resume.coordinate(ticket.id))["status"] == "COMPLETED"


async def test_acceptance_commit_before_checkpoint_is_not_repeated(restart, monkeypatch):
    ticket = await create()
    await approve(ticket.id)
    original = graph.execute

    async def crash_after_commit(state):
        await original(state)
        raise RuntimeError("simulated process death before checkpoint")

    with monkeypatch.context() as patch:
        patch.setattr(graph, "execute", crash_after_commit)
        first = await restart()
        assert (await resume.coordinate(ticket.id))["state"] == "deferred"
        assert await status(ticket.id) == "COMPLETED"
        assert (await snapshot(first, ticket.id)).next == ("execute",)
    async with db.SessionLocal() as session:
        reference = (await session.scalar(select(ExchangeAcceptance))).reference_code
    runtime = await restart()
    assert (await resume.coordinate(ticket.id))["status"] == "COMPLETED"
    assert not (await snapshot(runtime, ticket.id)).next
    assert await count(ExchangeAcceptance) == 1
    assert (await TicketService().detail(ALICE, ticket.id)).reference_code == reference


@pytest.mark.parametrize("finish", ["cancel", "refund"])
async def test_terminal_fact_ends_old_interrupt_after_restart(restart, finish):
    ticket = await create()
    await restart()
    await resume.coordinate(ticket.id)
    if finish == "cancel":
        await TicketService().cancel(ALICE, ticket.id, RequestInput(request_id="cancel"))
        expected = "CANCELLED"
    else:
        result = await _call(_ctx(ALICE.sub), "issue_refund", order_id=ticket.order_id)
        assert result["decision"] == "approved"
        expected = "REJECTED"
    runtime = await restart()
    assert (await resume.coordinate(ticket.id))["status"] == expected
    assert not (await snapshot(runtime, ticket.id)).next
    assert await count(ExchangeAcceptance) == 0


async def test_api_receipts_replay_original_status_and_enforce_scope(client, restart):
    ticket = await create()
    await restart()
    await resume.coordinate(ticket.id)
    path = f"/api/admin/tickets/{ticket.id}/decision"
    body = {"request_id": "approve", "decision": "approved", "comment": "同意"}
    response = await client.post(path, headers=admin_auth(), json=body)
    assert response.status_code == 202 and response.json()["status"] == "APPROVED"
    assert await status(ticket.id) == "COMPLETED"
    assert (await client.post(path, headers=admin_auth(), json=body)).json() == response.json()
    assert (
        await client.post(path, headers=admin_auth(), json=body | {"comment": "changed"})
    ).status_code == 409
    assert (await client.post(path, headers=auth(ALICE.sub), json=body)).status_code == 403
    scoped = {"Authorization": "Bearer " + mint_token("admin-1", ROLE_ADMIN, scope="CUST-002")}
    assert (await client.post(path, headers=scoped, json=body)).status_code == 404
    recovery = f"/api/admin/tickets/{ticket.id}/resume"
    assert (
        await client.post(recovery, headers=auth(ALICE.sub), json={"request_id": "resume"})
    ).status_code == 403
    assert (
        await client.post(recovery, headers=scoped, json={"request_id": "resume"})
    ).status_code == 404
    assert await count(ExchangeAcceptance) == 1


async def test_customer_write_endpoints_keep_durable_receipts(client, restart):
    ticket = await TicketService().create_or_find_exchange(
        ALICE,
        "ORD-1001",
        ExchangeInput(description="想换货"),
        start_open=True,
    )
    await restart()
    await resume.coordinate(ticket.id)
    path = f"/api/me/tickets/{ticket.id}"
    material = {
        "description": "右耳无声",
        "reason_type": "defect",
        "requested_change": "更换同款",
        "request_id": "supplement",
    }
    response = await client.post(path + "/supplement", headers=auth(ALICE.sub), json=material)
    assert response.status_code == 202 and response.json()["status"] == "PENDING_REVIEW"
    cancelled = await client.post(
        path + "/cancel", headers=auth(ALICE.sub), json={"request_id": "cancel"}
    )
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "CANCELLED"
    assert (
        await client.post(path + "/supplement", headers=auth(ALICE.sub), json=material)
    ).json() == response.json()
    assert (
        await client.post(path + "/cancel", headers=auth("CUST-002"), json={"request_id": "cancel"})
    ).status_code == 404
    assert await count(ExchangeAcceptance) == 0


@pytest.mark.parametrize("failure", ["timeout", "deadlock", "disconnect"])
async def test_startup_failure_preserves_approved_then_execution_can_retry(
    restart, monkeypatch, failure
):
    ticket = await create()
    await approve(ticket.id)
    runtime = await restart()

    async def unavailable(*args, **kwargs):
        if failure == "deadlock":
            import asyncpg

            raise DBAPIError("accept", {}, asyncpg.DeadlockDetectedError("simulated deadlock"))
        if failure == "disconnect":
            raise DBAPIError(
                "accept", {}, Exception("simulated disconnect"), connection_invalidated=True
            )
        raise TimeoutError("simulated outage")

    with monkeypatch.context() as patch:
        patch.setattr(runtime.graph, "aget_state", unavailable)
        assert (await resume.coordinate(ticket.id))["state"] == "deferred"
        assert await status(ticket.id) == "APPROVED"
    with monkeypatch.context() as patch:
        patch.setattr(TicketService, "accept_exchange", unavailable)
        assert (await resume.coordinate(ticket.id))["status"] == "FAILED_RETRYABLE"
    await restart()
    assert (await resume.coordinate(ticket.id))["status"] == "COMPLETED"
    assert await count(ExchangeAcceptance) == 1


async def test_unrecoverable_failure_requires_manual_resolution(client, restart, monkeypatch):
    ticket = await create()
    await approve(ticket.id)
    await restart()

    async def broken(*args, **kwargs):
        raise ValueError("simulated invalid execution")

    monkeypatch.setattr(TicketService, "accept_exchange", broken)
    assert (await resume.coordinate(ticket.id))["status"] == "FAILED_TERMINAL"
    response = await client.post(
        f"/api/admin/tickets/{ticket.id}/resume",
        headers=admin_auth(),
        json={"request_id": "manual"},
    )
    assert response.status_code == 409 and "MANUAL_REQUIRED" in str(response.json())
    assert await count(ExchangeAcceptance) == 0


async def test_timed_out_wakeup_keeps_receipt_and_replay_can_finish(client, restart, monkeypatch):
    from app import redis as shared

    ticket = await create()
    await restart()
    path = f"/api/admin/tickets/{ticket.id}/decision"
    body = {"decision": "approved", "request_id": "bounded"}

    async def blocked(*args):
        await asyncio.Event().wait()

    with monkeypatch.context() as patch:
        patch.setattr(get_settings(), "workflow_timeout_s", 0.05)
        patch.setattr(resume, "drive", blocked)
        response = await asyncio.wait_for(client.post(path, headers=admin_auth(), json=body), 3)
    assert response.status_code == 202 and response.json()["status"] == "APPROVED"
    assert await status(ticket.id) == "APPROVED"
    assert await shared.get_redis().get(f"workflow:{ticket.id}") is None
    replay = await client.post(path, headers=admin_auth(), json=body)
    assert replay.json() == response.json()
    assert await status(ticket.id) == "COMPLETED" and await count(ExchangeAcceptance) == 1


async def test_redis_lock_serializes_coordinators_and_checks_owner(restart, monkeypatch):
    from app import redis as shared

    ticket = await create()
    await restart()
    entered, release = asyncio.Event(), asyncio.Event()
    original = resume.drive

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(resume, "drive", delayed)
    running = asyncio.create_task(resume.coordinate(ticket.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await resume.coordinate(ticket.id))["state"] == "busy"
        assert not await shared.release_workflow_lock(ticket.id, "wrong-owner")
        assert await shared.acquire_workflow_lock(ticket.id, 45) is None
    finally:
        release.set()
        await running
    token = await shared.acquire_workflow_lock(ticket.id, 45)
    assert token is not None
    await shared.release_workflow_lock(ticket.id, token)


@pytest.mark.usefixtures("setup_chat")
async def test_chat_creation_starts_workflow_and_followup_only_reads(restart, monkeypatch):
    runtime = await restart()
    chat.use_model(
        monkeypatch,
        chat.intent("exchange", order="ORD-EX-1001", needs_order=True),
        chat.ai(call=("get_order", {"order_id": "ORD-EX-1001"})),
        chat.ai("订单已核实"),
        chat.ai(call=("search_knowledge", {"query": "HP-01 左耳无声怎么换货"})),
        chat.proposal(),
    )
    events = await chat.turn("ORD-EX-1001 耳机左耳无声，我要换同型号")
    ticket_event = next(event for event in events if event["kind"] == "ticket_update")
    ticket_id = ticket_event["ticket_id"]
    assert ticket_event["status"] == "PENDING_REVIEW"
    assert any(
        event["kind"] == "workflow_status" and event["state"] == "interrupted" for event in events
    )
    assert "本轮仅记录申请" in chat.final(events)
    before = await snapshot(runtime, ticket_id)

    async def must_not_resume(*args):
        raise AssertionError("read-only followup must not resume workflow")

    monkeypatch.setattr(resume, "coordinate", must_not_resume)
    chat.use_model(monkeypatch, chat.intent("ticket_followup", ticket=ticket_id))
    followup = await chat.turn(f"查询 {ticket_id} 的进度")  # a new conversation
    assert ticket_id in chat.final(followup)
    assert (await snapshot(runtime, ticket_id)).config == before.config
    assert await chat.count_tickets() == 1 and await count(ExchangeAcceptance) == 0
