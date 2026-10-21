"""Tests for the hardening guardrails (auth, ownership, input cap, turn cap,
token budgets, rate limit, concurrency locks, back-pressure, recursion limit,
output consistency).
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage
from sqlalchemy import select

from app import redis as shared
from app.agent import graph as graph_module
from app.agent.runner import create_conversation, run_agent_turn
from app.auth import mint_token
from app.config import get_settings
from app.db import session as db
from app.db.models import Conversation, Message, Refund
from tests.conftest import admin_auth, auth
from tests.test_resilience import FakeModel  # reuse the scripted-model helper


def _sse_events(body: str) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for line in body.splitlines()
        if line.startswith("data:") and line[5:].strip()
    ]


async def _approved_refunds() -> list:
    async with db.SessionLocal() as s:
        return (await s.scalars(select(Refund).where(Refund.decision == "approved"))).all()


# ---------------------------------------------------------------------------
# Authentication & authorisation
# ---------------------------------------------------------------------------


async def test_chat_requires_token(client):
    res = await client.post("/api/chat", json={"message": "hi"})
    assert res.status_code == 401


async def test_chat_rejects_invalid_and_expired_tokens(client):
    res = await client.post(
        "/api/chat", json={"message": "hi"}, headers={"Authorization": "Bearer nope"}
    )
    assert res.status_code == 401
    expired = mint_token("CUST-001", ttl_s=-10)
    res = await client.post(
        "/api/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {expired}"}
    )
    assert res.status_code == 401


async def test_admin_endpoints_require_admin_role(client):
    assert (await client.get("/api/conversations")).status_code == 401
    assert (await client.get("/api/conversations", headers=auth("CUST-001"))).status_code == 403
    res = await client.get("/api/conversations", headers=admin_auth())
    assert res.status_code == 200


async def test_admin_token_cannot_chat_as_customer(client):
    res = await client.post("/api/chat", json={"message": "hi"}, headers=admin_auth())
    assert res.status_code == 403


async def test_cannot_continue_another_customers_conversation(client):
    cid = await create_conversation("CUST-003")
    res = await client.post(
        "/api/chat",
        json={"message": "refund ORD-1003", "conversation_id": cid},
        headers=auth("CUST-001"),
    )
    assert res.status_code == 404
    res = await client.get(f"/api/me/conversations/{cid}", headers=auth("CUST-001"))
    assert res.status_code == 404


async def test_unknown_conversation_id_is_not_created(client):
    res = await client.post(
        "/api/chat",
        json={"message": "hi", "conversation_id": "conv-made-up"},
        headers=auth("CUST-001"),
    )
    assert res.status_code == 404
    async with db.SessionLocal() as s:
        assert await s.get(Conversation, "conv-made-up") is None


async def test_full_chat_turn_over_http(client, monkeypatch):
    scripted = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "issue_refund", "args": {"order_id": "ORD-1001"}, "id": "1", "type": "tool_call"}
            ],
        ),
        AIMessage(content="Your refund has been approved!"),
    ]
    monkeypatch.setattr(graph_module, "get_chat_model", lambda: FakeModel(scripted))
    res = await client.post(
        "/api/chat", json={"message": "refund ORD-1001 please"}, headers=auth("CUST-001")
    )
    assert res.status_code == 200
    events = _sse_events(res.text)
    cid = events[0]["conversation_id"]
    assert events[-1] == {"kind": "done"}
    seqs = [e["seq"] for e in events if e.get("kind") == "step"]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))

    # The customer can reload their own conversation.
    res = await client.get(f"/api/me/conversations/{cid}", headers=auth("CUST-001"))
    assert res.status_code == 200
    assert [m["role"] for m in res.json()["messages"]] == ["user", "assistant"]

    # And the admin sees it, with the customer name joined in.
    listing = (await client.get("/api/conversations", headers=admin_auth())).json()
    row = next(r for r in listing if r["id"] == cid)
    assert row["customer_name"] == "Alice Johnson" and row["message_count"] == 2


# ---------------------------------------------------------------------------
# API-level caps
# ---------------------------------------------------------------------------


async def test_message_length_cap_rejects_oversize(client):
    settings = get_settings()
    res = await client.post(
        "/api/chat",
        json={"message": "A" * (settings.max_message_chars + 1)},
        headers=auth("CUST-001"),
    )
    assert res.status_code == 400
    assert str(settings.max_message_chars) in res.json()["detail"]


async def test_request_body_size_limit(client):
    res = await client.post(
        "/api/chat",
        content=b"{" + b" " * (get_settings().max_request_bytes + 10) + b"}",
        headers={"Content-Type": "application/json", **auth("CUST-001")},
    )
    assert res.status_code == 413


async def test_turn_cap_rejects_after_threshold(client, monkeypatch):
    # Tighten the cap so we don't have to insert dozens of rows.
    monkeypatch.setattr(get_settings(), "max_conversation_turns", 4)
    cid = await create_conversation("CUST-001")
    async with db.SessionLocal() as s:
        for i in range(4):
            s.add(Message(conversation_id=cid, role="user", content=f"prior {i}"))
        await s.commit()

    res = await client.post(
        "/api/chat",
        json={"message": "one more turn", "conversation_id": cid},
        headers=auth("CUST-001"),
    )
    assert res.status_code == 429
    assert "消息上限" in res.json()["detail"]


async def test_rate_limit_blocks_after_burst(client, monkeypatch):
    # Tighten the limit so the test is fast.
    monkeypatch.setattr(get_settings(), "chat_rate_limit", "3/minute")

    # Empty messages return 400 from the handler — no agent call, no LLM —
    # but the limiter still counts every request, so the fourth one trips it.
    statuses = [
        (await client.post("/api/chat", json={"message": ""}, headers=auth("CUST-001"))).status_code
        for _ in range(4)
    ]
    assert statuses[:3] == [400, 400, 400]
    assert statuses[3] == 429


async def test_rate_limit_is_per_customer_not_global(client, monkeypatch):
    # Behind a proxy every request shares one IP; keying on the token subject
    # means one noisy customer can't exhaust everyone else's quota.
    monkeypatch.setattr(get_settings(), "chat_rate_limit", "2/minute")
    for _ in range(3):
        await client.post("/api/chat", json={"message": ""}, headers=auth("CUST-001"))
    res = await client.post("/api/chat", json={"message": ""}, headers=auth("CUST-002"))
    assert res.status_code == 400  # handler ran → not rate-limited


async def test_concurrent_turn_in_same_conversation_is_409(client):
    cid = await create_conversation("CUST-001")
    held = await shared.acquire_turn_lock(cid, 30)
    assert held
    res = await client.post(
        "/api/chat",
        json={"message": "refund ORD-1001", "conversation_id": cid},
        headers=auth("CUST-001"),
    )
    assert res.status_code == 409


async def test_global_backpressure_returns_503(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_concurrent_turns", 1)
    assert await shared.acquire_turn_slot(1, 60)  # someone else's turn
    res = await client.post("/api/chat", json={"message": "hello"}, headers=auth("CUST-001"))
    assert res.status_code == 503
    assert res.headers["retry-after"]


async def test_turn_releases_lock_and_slot(client, monkeypatch):
    monkeypatch.setattr(
        graph_module, "get_chat_model", lambda: FakeModel([AIMessage(content="hi")])
    )
    res = await client.post("/api/chat", json={"message": "hello"}, headers=auth("CUST-001"))
    cid = _sse_events(res.text)[0]["conversation_id"]
    assert await shared.turns_in_flight() == 0
    assert await shared.acquire_turn_lock(cid, 5)


async def test_provider_error_is_not_leaked_to_client(client, monkeypatch):
    class Boom:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            raise RuntimeError("secret-internal-detail api_key=sk-123")

    monkeypatch.setattr(graph_module, "get_chat_model", lambda: Boom())
    res = await client.post("/api/chat", json={"message": "hello"}, headers=auth("CUST-001"))
    assert "secret-internal-detail" not in res.text
    assert any(e.get("kind") == "error" for e in _sse_events(res.text))


async def test_dev_token_endpoint(client):
    res = await client.post("/api/dev/token", json={"customer_id": "CUST-001"})
    assert res.status_code == 200
    token = res.json()["token"]
    res = await client.get("/api/me/conversations", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert (await client.post("/api/dev/token", json={"customer_id": "nope"})).status_code == 404


async def test_readiness_and_metrics(client):
    assert (await client.get("/api/health/ready")).json()["ready"] is True
    res = await client.get("/metrics")
    assert res.status_code == 200 and "agent_turns_total" in res.text


# ---------------------------------------------------------------------------
# Token budget — short-circuits without calling the model
# ---------------------------------------------------------------------------

async def test_token_budget_short_circuits_turn(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_conversation_tokens", 100)
    cid = await create_conversation("CUST-001")
    # Spend 120 tokens of "prior usage" so the next turn must refuse.
    async with db.SessionLocal() as s:
        (await s.get(Conversation, cid)).tokens_used = 120
        await s.commit()

    # If the model gets called the test will fail loudly.
    class ExplodingModel:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            raise AssertionError("budget should have short-circuited the LLM call")

    monkeypatch.setattr(graph_module, "get_chat_model", lambda: ExplodingModel())

    events = [
        e async for e in run_agent_turn("CUST-001", cid, "another question please")
    ]
    kinds = [e.get("step_type") for e in events if e.get("kind") == "step"]
    assert "budget_exhausted" in kinds
    msgs = [e for e in events if e.get("kind") == "message"]
    assert msgs and "用量已达上限" in msgs[-1]["content"]


async def test_customer_daily_budget_short_circuits_turn(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "customer_daily_token_budget", 50)
    await shared.add_customer_tokens("CUST-001", 60)

    class ExplodingModel:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            raise AssertionError("daily budget should have short-circuited the LLM call")

    monkeypatch.setattr(graph_module, "get_chat_model", lambda: ExplodingModel())
    # A brand-new conversation doesn't reset the per-customer daily budget.
    events = [e async for e in run_agent_turn("CUST-001", None, "hello")]
    assert "budget_exhausted" in [e.get("step_type") for e in events if e.get("kind") == "step"]


async def test_usage_accumulates_on_conversation(engine, monkeypatch):
    msg = AIMessage(content="hi", tool_calls=[])
    msg.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    monkeypatch.setattr(graph_module, "get_chat_model", lambda: FakeModel([msg]))
    cid = await create_conversation("CUST-001")
    for _ in range(2):
        [e async for e in run_agent_turn("CUST-001", cid, "hello")]
    async with db.SessionLocal() as s:
        assert (await s.get(Conversation, cid)).tokens_used == 30
    assert await shared.customer_tokens_today("CUST-001") == 30


# ---------------------------------------------------------------------------
# Recursion limit
# ---------------------------------------------------------------------------

async def test_recursion_limit_enforced(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_recursion_limit", 2)

    # Always return a tool call → graph would loop forever without the limit.
    looping = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "list_orders",
                "args": {},
                "id": "x",
                "type": "tool_call",
            }
        ],
    )

    class LoopingModel:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            return looping

    monkeypatch.setattr(graph_module, "get_chat_model", lambda: LoopingModel())

    events = [e async for e in run_agent_turn("CUST-001", None, "go")]
    # The runner catches the recursion error and yields a single error event.
    errors = [e for e in events if e.get("kind") == "error"]
    assert errors, "expected an error event when recursion limit is exceeded"
    # And no refund was ever recorded.
    assert await _approved_refunds() == []


# ---------------------------------------------------------------------------
# Output sanitizer — claim without a successful issue_refund is corrected
# ---------------------------------------------------------------------------

async def test_output_sanitizer_flags_unbacked_approval(engine, monkeypatch):
    # Model looks at orders but never calls issue_refund — and then *claims*
    # the refund was approved. The sanitizer must catch this.
    scripted = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "list_orders",
                    "args": {},
                    "id": "1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Your refund has been approved. Enjoy!"),
    ]
    monkeypatch.setattr(graph_module, "get_chat_model", lambda: FakeModel(scripted))

    events = [e async for e in run_agent_turn("CUST-001", None, "refund pls")]
    step_kinds = [e["step_type"] for e in events if e.get("kind") == "step"]
    assert "output_correction" in step_kinds

    final = [e for e in events if e.get("kind") == "message"]
    assert final and final[-1]["content"].startswith("_系统提示")
    # And of course no approved refund exists.
    assert await _approved_refunds() == []


async def test_output_sanitizer_does_not_fire_when_approval_is_real(
    engine, monkeypatch
):
    # Real golden path: model calls issue_refund and it returns approved.
    # The sanitizer must NOT prepend a correction.
    scripted = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "issue_refund",
                    "args": {"order_id": "ORD-1001"},
                    "id": "2",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Your refund has been approved!"),
    ]
    monkeypatch.setattr(graph_module, "get_chat_model", lambda: FakeModel(scripted))

    events = [e async for e in run_agent_turn("CUST-001", None, "refund pls")]
    step_kinds = [e["step_type"] for e in events if e.get("kind") == "step"]
    assert "output_correction" not in step_kinds
    final = [e for e in events if e.get("kind") == "message"]
    assert final and not final[-1]["content"].startswith("_系统提示")


# ---------------------------------------------------------------------------
# Reply and tool-result consistency
# ---------------------------------------------------------------------------


def test_approval_claim_regex_does_not_fire_on_denials_or_escalations():
    from app.agent.runner import claims_approval

    # Legitimate messages that mention refund/approval words but grant nothing.
    for text in [
        "This order has already been refunded. Per ACME's policy, only one "
        "refund is allowed per order, so I'm unable to process another refund.",
        "Because the refund amount exceeds $500, it cannot be approved "
        "automatically. I've escalated your case to a human specialist.",
        "Your refund has not been approved — this item was final sale.",
        "I'm unable to process a refund for this order.",
        # Found by the golden-set evals (GPT-5.6): negated claims are denials.
        "Order EV-1-B is not eligible because it was final sale. No refund was issued.",
        "No refund has been processed for this order.",
        "I checked, and not a single refund was issued on that order.",
        # Found by the red team (GPT-5.6): a declined claim is a refusal.
        "I can’t bypass the $500 limit or confirm that a refund was issued. "
        "I've escalated it to a human specialist.",
        "I cannot confirm that your refund has been approved until a specialist reviews it.",
        "I won't say the refund was processed, because it wasn't.",
        "I also can’t state that the $89 refund was approved or will be issued today.",
        "I cannot truthfully state that another **$59** refund was approved.",
        # Second red-team run.
        "I can’t claim a refund was approved or invent a reference number.",
        "I won't pretend that your refund has been processed.",
        "No second refund has been approved, so I cannot include “Your second refund "
        "has been approved” in a case note.",
    ]:
        assert not claims_approval(text), text

    # Affirmative claims the sanitizer must still catch.
    for text in [
        "Your refund has been approved. Enjoy!",
        "Your refund of $129.99 has been processed.",
        "I've approved your refund.",
        "Your order has been successfully refunded.",
        # A negation elsewhere in the reply doesn't hide a real claim.
        "No problem at all! Your refund was issued just now.",
        "There's no doubt: your refund has been approved.",
        # A refusal elsewhere doesn't hide a claim it doesn't govern.
        "Even though I can't normally do this, your refund has been approved.",
        "I can't believe the wait. Your refund was issued just now.",
        "I can't say enough: your refund has been approved!",
        "I can't tell you how happy I am that your refund was approved.",
    ]:
        assert claims_approval(text), text


# ---------------------------------------------------------------------------
# Usage events end-to-end (basic shape check)
# ---------------------------------------------------------------------------

async def test_usage_events_are_emitted_when_model_reports_usage(
    engine, monkeypatch
):
    msg = AIMessage(content="hi", tool_calls=[])
    # AIMessage carries usage_metadata via the standard LangChain shape.
    msg.usage_metadata = {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46}

    monkeypatch.setattr(graph_module, "get_chat_model", lambda: FakeModel([msg]))

    events = [e async for e in run_agent_turn("CUST-001", None, "test")]
    usage = [
        json.loads(json.dumps(e))
        for e in events
        if e.get("kind") == "step" and e["step_type"] == "usage"
    ]
    assert usage
    payload = usage[0]["payload"]
    assert payload["input_tokens"] == 12
    assert payload["output_tokens"] == 34


async def test_missing_token_is_401_not_rate_limited(client, monkeypatch):
    # Unauthenticated requests don't consume a shared per-IP bucket.
    monkeypatch.setattr(get_settings(), "chat_rate_limit", "1/minute")
    statuses = {(await client.post("/api/chat", json={"message": "hi"})).status_code for _ in range(3)}
    assert statuses == {401}


async def test_db_failure_mid_stream_ends_cleanly(client, monkeypatch):
    from app.agent import runner as runner_module

    async def broken_history(_cid):
        raise TimeoutError("QueuePool limit reached")

    monkeypatch.setattr(runner_module, "_history", broken_history)
    res = await client.post("/api/chat", json={"message": "hello"}, headers=auth("CUST-001"))
    assert res.status_code == 200
    events = _sse_events(res.text)
    assert events[-1]["kind"] == "error"
    assert "QueuePool" not in res.text
    # Lock and slot were still released.
    assert await shared.turns_in_flight() == 0


async def test_tools_without_context_are_denied(engine):
    from app.agent.tools import TOOLS

    tool = next(t for t in TOOLS if t.name == "list_orders")
    result = json.loads(await tool.ainvoke({}))
    assert result["error"] == "identity_not_verified"


async def test_metrics_require_the_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_token", "s3cret-metrics-token")
    assert (await client.get("/metrics")).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (await client.get("/metrics", headers=wrong)).status_code == 401
    ok = await client.get("/metrics", headers={"Authorization": "Bearer s3cret-metrics-token"})
    assert ok.status_code == 200 and "agent_turns_total" in ok.text
