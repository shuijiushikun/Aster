"""Run golden-set conversations through the real agent and record what happened.

Each case gets its own customer with one order per policy branch (below), plus
a second customer whose order is used for ownership attacks. Case text refers
to orders by key (`{A}`, `{X}`, ...), so cases stay readable and every attempt
is isolated: a retry gets brand-new ids.

The expected outcome for an order comes from its branch, never from the case
file, so a case can't encode the wrong policy. Cases only override it when the
conversation shouldn't reach a decision at all (e.g. off-topic requests).
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select

from app.agent.runner import run_agent_turn
from app.db import session as db
from app.db.models import Customer, Order, Refund, utcnow

CASES_DIR = Path(__file__).parent / "cases"


@dataclass(frozen=True)
class Branch:
    product: str
    amount: str
    delivered_days_ago: float | None  # None = not delivered yet
    expected: str  # approved | denied | escalated
    final_sale: bool = False
    refunded: bool = False


# Key -> policy branch. Amounts and dates sit on the rule boundaries.
BRANCHES: dict[str, Branch] = {
    "A": Branch("Noise-Cancelling Headphones", "129.99", 10, "approved"),
    "B": Branch("Clearance Hoodie", "39.99", 5, "denied", final_sale=True),
    "C": Branch("Ultrabook Laptop", "1299.00", 7, "escalated"),
    "D": Branch("Desk Lamp", "89.00", 45, "denied"),
    "E": Branch("Mechanical Keyboard", "59.00", 8, "denied", refunded=True),
    "F": Branch("Standing Desk Mat", "75.00", None, "denied"),
    "G": Branch("Espresso Machine", "500.00", 6, "approved"),  # exactly the limit
    "H": Branch("Yoga Mat", "49.00", 30.04, "approved"),  # last day of the window
    "I": Branch("Water Bottle", "49.00", 31.04, "denied"),  # first day outside
    "J": Branch("Designer Jacket", "899.00", 5, "denied", final_sale=True),  # final sale wins
}
OTHER_KEY = "X"  # another customer's (refundable) order: must be refused

OUTCOMES = {"approved", "denied", "escalated", "refused", "none"}
DECISION_TOOLS = ("issue_refund", "escalate_to_human")


@dataclass
class Case:
    id: str
    turns: list[str]
    targets: list[str]
    tags: list[str]
    expect: dict[str, str] = field(default_factory=dict)
    live_only: bool = False
    note: str = ""

    def expected(self, key: str) -> set[str]:
        if key in self.expect:
            return {self.expect[key]}
        if key == OTHER_KEY:
            # Refused by the ownership guard, or never looked up at all.
            return {"refused", "none"}
        return {BRANCHES[key].expected}


def load_cases(paths: list[Path] | None = None) -> list[Case]:
    cases: list[Case] = []
    for path in paths or sorted(CASES_DIR.glob("*.yaml")):
        for raw in yaml.safe_load(path.read_text(encoding="utf-8")):
            turns = raw["turns"] if "turns" in raw else [raw["text"]]
            case = Case(
                id=raw["id"],
                turns=turns,
                targets=raw.get("targets", []),
                tags=[path.stem, *raw.get("tags", [])],
                expect=raw.get("expect", {}),
                live_only=raw.get("live_only", False),
                note=raw.get("note", ""),
            )
            unknown = set(case.targets) - set(BRANCHES) - {OTHER_KEY}
            bad = set(case.expect.values()) - OUTCOMES
            if unknown or bad:
                raise ValueError(f"{path.name}:{case.id}: bad targets {unknown} / outcomes {bad}")
            cases.append(case)
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case ids: {sorted(dupes)}")
    return cases


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_numbers = itertools.count(100)


@dataclass
class Fixture:
    customer_id: str
    other_id: str
    order_ids: dict[str, str]  # key -> order id (incl. X)

    def render(self, text: str) -> str:
        return text.format_map(self.order_ids)


async def create_fixture() -> Fixture:
    # Ids match the scripted model's order pattern (LETTERS-NNN-SUFFIX).
    n = next(_numbers)
    cid, other = f"EV-{n:03d}", f"EVX-{n:03d}"
    now = utcnow()
    orders = []
    for key, b in BRANCHES.items():
        delivered = None if b.delivered_days_ago is None else now - timedelta(days=b.delivered_days_ago)
        orders.append(
            Order(
                id=f"{cid}-{key}",
                customer_id=cid,
                product_name=b.product,
                amount=Decimal(b.amount),
                status="delivered" if delivered else "shipped",
                order_date=(delivered or now) - timedelta(days=3),
                delivered_date=delivered,
                is_final_sale=b.final_sale,
                refunded=b.refunded,
            )
        )
    orders.append(
        Order(
            id=f"{other}-A",
            customer_id=other,
            product_name="Smart Watch",
            amount=Decimal("210.00"),
            status="delivered",
            order_date=now - timedelta(days=9),
            delivered_date=now - timedelta(days=6),
        )
    )
    async with db.SessionLocal() as s:
        s.add(Customer(id=cid, name=f"Eval Customer {n}", email=f"ev{n}@eval.test"))
        s.add(Customer(id=other, name=f"Other Customer {n}", email=f"evx{n}@eval.test"))
        s.add_all(orders)
        await s.commit()
    ids = {key: f"{cid}-{key}" for key in BRANCHES} | {OTHER_KEY: f"{other}-A"}
    return Fixture(cid, other, ids)


# ---------------------------------------------------------------------------
# Running a case
# ---------------------------------------------------------------------------


@dataclass
class Run:
    case: Case
    fixture: Fixture
    turns: list[dict]  # [{"user": str, "assistant": str, "events": [...]}]
    ledger: list[dict]  # refund rows for this case's orders
    error: str | None = None

    @property
    def steps(self) -> list[dict]:
        return [e for t in self.turns for e in t["events"] if e.get("kind") == "step"]

    def tool_calls(self) -> list[dict]:
        return [s["payload"] for s in self.steps if s["step_type"] == "tool_call"]

    def tool_results(self) -> list[dict]:
        return [
            s["payload"]
            for s in self.steps
            if s["step_type"] in ("policy_eval", "decision", "tool_result")
        ]

    def trace_ids(self) -> list[str]:
        return [s["payload"]["trace_id"] for s in self.steps if s["step_type"] == "trace"]

    def tokens(self) -> tuple[int, int]:
        usage = [s["payload"] for s in self.steps if s["step_type"] == "usage"]
        return (
            sum(u.get("input_tokens", 0) for u in usage),
            sum(u.get("output_tokens", 0) for u in usage),
        )

    def transcript(self) -> str:
        lines = []
        for t in self.turns:
            lines.append(f"Customer: {t['user']}")
            lines.append(f"Agent: {t['assistant']}")
        return "\n".join(lines)

    def outcome(self, key: str) -> str:
        """What actually happened to order `key`, from the ledger first, then tool results."""
        order_id = self.fixture.order_ids[key]
        rows = [r["decision"] for r in self.ledger if r["order_id"] == order_id]
        if "approved" in rows:
            return "approved"
        if "escalated" in rows:
            return "escalated"
        pattern = re.compile(rf"(?<![\w-]){re.escape(order_id)}(?![\w-])")
        about = [r for r in self.tool_results() if pattern.search(json.dumps(r))]
        errors = {r["result"].get("error") for r in about if isinstance(r.get("result"), dict)}
        if errors & {"ownership_mismatch", "order_not_found"}:
            return "refused"
        decisions = {r["result"].get("decision") for r in about if isinstance(r.get("result"), dict)}
        if "denied" in decisions:
            return "denied"
        return "none"


async def run_case(case: Case) -> Run:
    fixture = await create_fixture()
    turns: list[dict] = []
    conversation_id = None
    error = None
    for text in case.turns:
        user = fixture.render(text)
        events = [e async for e in run_agent_turn(fixture.customer_id, conversation_id, user)]
        conversation_id = next(
            (e["conversation_id"] for e in events if e.get("kind") == "conversation"),
            conversation_id,
        )
        reply = next(
            (e["content"] for e in reversed(events) if e.get("kind") == "message"), ""
        )
        failed = next((e["message"] for e in events if e.get("kind") == "error"), None)
        turns.append({"user": user, "assistant": reply, "events": events})
        if failed:
            error = failed
            break
    async with db.SessionLocal() as s:
        rows = (
            await s.scalars(
                select(Refund).where(Refund.order_id.in_(list(fixture.order_ids.values())))
            )
        ).all()
    ledger = [{"order_id": r.order_id, "decision": r.decision} for r in rows]
    return Run(case, fixture, turns, ledger, error)
