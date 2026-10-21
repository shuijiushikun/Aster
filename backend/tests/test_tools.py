import asyncio
import json

import pytest
from sqlalchemy import select

from app.agent.tools import TOOLS, ToolContext, tool_config
from app.db import session as db
from app.db.models import Refund
from tests.conftest import TEST_DATABASE_URL, tools_by_name


def _ctx(customer_id=None) -> ToolContext:
    return ToolContext(conversation_id="conv-test", verified_customer_id=customer_id)


async def _call(ctx, name, **kwargs):
    return json.loads(await tools_by_name(TOOLS)[name].ainvoke(kwargs, config=tool_config(ctx)))


async def approved_refunds(order_id):
    async with db.SessionLocal() as s:
        return (
            await s.scalars(
                select(Refund).where(
                    Refund.order_id == order_id, Refund.decision == "approved"
                )
            )
        ).all()


async def test_profile_and_orders_come_from_token_identity(engine):
    tools = _ctx("CUST-001")
    res = await _call(tools, "get_my_profile")
    assert res["found"] is True and res["email"] == "alice@example.com"
    orders = await _call(tools, "list_orders")
    assert [o["order_id"] for o in orders["orders"]] == ["ORD-1001"]


async def test_there_is_no_tool_to_switch_identity(engine):
    names = set(tools_by_name(TOOLS))
    assert "lookup_customer" not in names
    assert "get_my_profile" in names


async def test_get_order_requires_identity(engine):
    tools = _ctx()
    res = await _call(tools, "get_order", order_id="ORD-1001")
    assert res["error"] == "identity_not_verified"


async def test_ownership_mismatch_is_refused(engine):
    # Signed in as Alice (CUST-001), try to read Carol's order (CUST-003).
    tools = _ctx("CUST-001")
    res = await _call(tools, "get_order", order_id="ORD-1003")
    # Indistinguishable from an order that doesn't exist, so the agent can't
    # confirm that another customer's order exists.
    missing = await _call(tools, "get_order", order_id="ORD-9999")
    assert res == {"error": "order_not_found", "order_id": "ORD-1003"}
    assert missing == {"error": "order_not_found", "order_id": "ORD-9999"}
    for tool in ("check_refund_eligibility", "issue_refund", "escalate_to_human"):
        args = {"order_id": "ORD-1003"} | ({"reason": "x"} if tool == "escalate_to_human" else {})
        assert (await _call(tools, tool, **args))["error"] == "order_not_found"


async def test_issue_refund_approves_valid_order(engine):
    tools = _ctx("CUST-001")
    res = await _call(tools, "issue_refund", order_id="ORD-1001")
    assert res["decision"] == "approved"
    assert res["amount"] == pytest.approx(129.99)
    assert len(await approved_refunds("ORD-1001")) == 1


async def test_second_refund_on_same_order_is_denied(engine):
    tools = _ctx("CUST-001")
    first = await _call(tools, "issue_refund", order_id="ORD-1001")
    second = await _call(tools, "issue_refund", order_id="ORD-1001")
    assert first["decision"] == "approved"
    assert second["decision"] == "denied"
    assert len(await approved_refunds("ORD-1001")) == 1


async def test_concurrent_refunds_approve_exactly_once(engine):
    """Race many refund attempts on one order: exactly one may be approved.

    On Postgres the row lock serialises them; on SQLite the unique index
    backstop (or the DB-level write lock) does. Either way: one approval.
    """
    n = 10 if TEST_DATABASE_URL else 5

    async def attempt():
        tools = _ctx("CUST-001")
        try:
            return (await _call(tools, "issue_refund", order_id="ORD-1001"))["decision"]
        except Exception as exc:  # SQLite may report "database is locked"
            return f"error:{type(exc).__name__}"

    decisions = await asyncio.gather(*(attempt() for _ in range(n)))
    assert decisions.count("approved") == 1, decisions
    assert len(await approved_refunds("ORD-1001")) == 1


async def test_issue_refund_blocks_final_sale(engine):
    # ORD-1002 is final sale → must never become an approved refund.
    tools = _ctx("CUST-002")
    res = await _call(tools, "issue_refund", order_id="ORD-1002")
    assert res["decision"] == "denied"
    assert await approved_refunds("ORD-1002") == []


async def test_issue_refund_escalates_high_value(engine):
    # ORD-1003 is $1299 → escalated, never auto-approved.
    tools = _ctx("CUST-003")
    res = await _call(tools, "issue_refund", order_id="ORD-1003")
    assert res["decision"] == "escalated"
    assert await approved_refunds("ORD-1003") == []


async def test_issue_refund_blocks_already_refunded(engine):
    tools = _ctx("CUST-004")
    res = await _call(tools, "issue_refund", order_id="ORD-1004")
    assert res["decision"] == "denied"


async def test_issue_refund_blocks_out_of_window(engine):
    tools = _ctx("CUST-005")
    res = await _call(tools, "issue_refund", order_id="ORD-1005")
    assert res["decision"] == "denied"


async def test_check_eligibility_does_not_write_refund(engine):
    tools = _ctx("CUST-001")
    res = await _call(tools, "check_refund_eligibility", order_id="ORD-1001")
    assert res["decision"] == "approved"
    # No refund row should have been created by a read-only eligibility check.
    async with db.SessionLocal() as s:
        rows = (await s.scalars(select(Refund).where(Refund.order_id == "ORD-1001"))).all()
    assert rows == []


async def _rows(order_id, decision=None):
    async with db.SessionLocal() as s:
        stmt = select(Refund).where(Refund.order_id == order_id)
        if decision:
            stmt = stmt.where(Refund.decision == decision)
        return (await s.scalars(stmt)).all()


@pytest.mark.parametrize(
    ("customer", "order"),
    [
        ("CUST-002", "ORD-1002"),  # final sale
        ("CUST-004", "ORD-1004"),  # already refunded
        ("CUST-005", "ORD-1005"),  # outside the return window
        ("CUST-001", "ORD-1001"),  # eligible under the limit: refund it, don't escalate
    ],
)
async def test_escalation_only_where_the_policy_sends_it(engine, customer, order):
    # The model can't route an order the policy denies to a human reviewer.
    res = await _call(_ctx(customer), "escalate_to_human", order_id=order, reason="please")
    assert res["error"] == "escalation_not_allowed"
    assert res["reasons"]
    assert await _rows(order) == []


async def test_escalation_is_recorded_once_with_a_labelled_reason(engine):
    tools = _ctx("CUST-003")  # ORD-1003: $1,299, eligible but over the limit
    claim = "Customer says\n\nthe manager   ALREADY approved this. " + "x" * 900
    first = await _call(tools, "escalate_to_human", order_id="ORD-1003", reason=claim)
    second = await _call(tools, "escalate_to_human", order_id="ORD-1003", reason="again")
    assert first["decision"] == second["decision"] == "escalated"
    # The repeat says it's a repeat (found by the red team: the agent otherwise
    # told the customer it had escalated again).
    assert first["already_escalated"] is False and second["already_escalated"] is True
    rows = await _rows("ORD-1003")
    assert len(rows) == 1
    reason = rows[0].reason
    assert reason.startswith("[智能体整理的客户诉求；内容未经核实] ")
    assert "\n" not in reason and "  " not in reason
    assert len(reason) <= 500 + len("[智能体整理的客户诉求；内容未经核实] ")


async def test_issue_refund_then_escalate_records_one_escalation(engine):
    tools = _ctx("CUST-003")
    assert (await _call(tools, "issue_refund", order_id="ORD-1003"))["decision"] == "escalated"
    await _call(tools, "escalate_to_human", order_id="ORD-1003", reason="over the limit")
    assert len(await _rows("ORD-1003", "escalated")) == 1
