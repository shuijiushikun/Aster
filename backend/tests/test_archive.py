"""Demo transcripts survive the sandbox purge, with triage flags and breach checks."""

import json
import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import session as db
from app.db.models import (
    Conversation,
    ConversationArchive,
    Customer,
    Message,
    Refund,
    utcnow,
)
from app.maintenance.retention import purge_archive, purge_demo


@pytest.fixture
def demo(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "fake_llm_latency_ms", 0)
    return settings


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def new_session(client) -> dict:
    res = await client.post("/api/demo/session")
    assert res.status_code == 200, res.text
    return res.json()


async def chat(client, token: str, message: str, conversation_id: str | None = None) -> str:
    body = {"message": message, "conversation_id": conversation_id}
    res = await client.post("/api/chat", json=body, headers=bearer(token))
    assert res.status_code == 200, res.text
    events = [json.loads(line[5:]) for line in res.text.splitlines() if line.startswith("data:")]
    return next(e["conversation_id"] for e in events if e["kind"] == "conversation")


def order(session: dict, scenario: str) -> str:
    return next(o["id"] for o in session["orders"] if o["scenario"] == scenario)


async def age(customer_id: str, hours: int = 48) -> None:
    async with db.SessionLocal() as s:
        await s.execute(
            update(Customer)
            .where(Customer.id == customer_id)
            .values(created_at=utcnow() - timedelta(hours=hours))
        )
        await s.commit()


async def archived(conversation_id: str) -> ConversationArchive | None:
    async with db.SessionLocal() as s:
        return await s.get(ConversationArchive, conversation_id)


async def test_purge_archives_the_full_trace_first(client, demo):
    a = await new_session(client)
    conv = await chat(client, a["token"], f"Please refund order {order(a, 'refundable')}")
    await chat(
        client, a["token"], f"Please refund order {order(a, 'final_sale')}", conv
    )
    await age(a["customer"]["id"])

    sandboxes, convs, breached = await purge_demo(24)
    assert (sandboxes, convs, breached) == (1, 1, 0)

    row = await archived(conv)
    assert row is not None and row.customer_id == a["customer"]["id"]
    assert row.turns == 2
    assert [(d["scenario"], d["decision"]) for d in row.decisions][0] == ("refundable", "approved")
    assert "injection" not in row.flags
    assert row.breach is False
    roles = [m["role"] for m in row.transcript["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    steps = {e["step_type"] for e in row.transcript["events"]}
    assert {"tool_call", "policy_eval"} <= steps
    assert len(row.transcript["orders"]) == 5

    # The live rows are gone; only the archive remains.
    async with db.SessionLocal() as s:
        assert await s.get(Conversation, conv) is None
        assert await s.scalar(select(func.count(Message.id))) == 0


async def test_forbidden_approval_is_a_logged_breach(client, demo, caplog):
    a = await new_session(client)
    conv = await chat(client, a["token"], "What orders do I have?")
    # Simulate the tool gate failing: an approval on the final-sale order.
    async with db.SessionLocal() as s:
        s.add(
            Refund(
                order_id=order(a, "final_sale"),
                amount=Decimal("10"),
                decision="approved",
                conversation_id=conv,
            )
        )
        await s.commit()
    await age(a["customer"]["id"])

    with caplog.at_level(logging.ERROR):
        assert (await purge_demo(24))[2] == 1
    row = await archived(conv)
    assert row.breach is True
    assert [b["kind"] for b in row.breaches] == ["forbidden_approval"]
    assert any("demo breach detected" in r.getMessage() for r in caplog.records)


async def test_reply_naming_another_sandbox_is_a_possible_leak(client, demo):
    a = await new_session(client)
    b = await new_session(client)
    conv = await chat(client, a["token"], "What orders do I have?")
    async with db.SessionLocal() as s:
        s.add(
            Message(
                conversation_id=conv,
                role="assistant",
                content=f"Order {order(b, 'refundable')} belongs to someone else.",
            )
        )
        await s.commit()
    await age(a["customer"]["id"])
    await purge_demo(24)

    row = await archived(conv)
    assert [x["kind"] for x in row.breaches] == ["possible_data_leak"]
    assert b["customer"]["id"] in row.breaches[0]["detail"]


async def test_archive_rows_expire_after_retention(client, demo):
    a = await new_session(client)
    conv = await chat(client, a["token"], "What orders do I have?")
    await age(a["customer"]["id"])
    await purge_demo(24)

    assert await purge_archive(90) == 0
    async with db.SessionLocal() as s:
        await s.execute(
            update(ConversationArchive).values(archived_at=utcnow() - timedelta(days=91))
        )
        await s.commit()
    assert await purge_archive(90) == 1
    assert await archived(conv) is None


async def test_meta_reports_transcript_retention(client, demo):
    body = (await client.get("/api/meta")).json()
    assert body["demo"]["transcript_retention_days"] == demo.retention_days
