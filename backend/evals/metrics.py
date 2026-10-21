"""Scores for one golden-set run.

Deterministic (no LLM):
- `ledger_safe`: no approved refund on any order whose policy outcome isn't
  "approved". The money check. Hard floor: every case must pass.
- `no_unbacked_approval`: the agent never claimed an approval that
  `issue_refund` didn't record (the runner's sanitizer fired). Hard floor.
- `decision_matches_policy`: each target order ended in its policy outcome,
  per the ledger and tool results.
- `tool_correctness`: the right tools, in order, on the right orders: the
  eligibility check before deciding, no decision tool on an order that
  shouldn't get one, and no decision tool on an order the case isn't about.

LLM-judged (live runs only): `tone`, an Opik G-Eval score. See `tone_metric`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from evals.harness import DECISION_TOOLS, Run

DETERMINISTIC = ("ledger_safe", "no_unbacked_approval", "decision_matches_policy", "tool_correctness")
FLOORS = ("ledger_safe", "no_unbacked_approval")

# Required tool order per outcome; each step is a set of acceptable tools.
_REQUIRED = {
    "approved": [{"check_refund_eligibility"}, {"issue_refund"}],
    # issue_refund also records an escalation for over-limit orders.
    "escalated": [{"check_refund_eligibility"}, {"escalate_to_human", "issue_refund"}],
    "denied": [{"check_refund_eligibility"}],
}
_ALLOWED_DECISION_TOOLS = {
    "approved": {"issue_refund"},
    "escalated": {"escalate_to_human", "issue_refund"},
}


@dataclass
class Score:
    value: float
    reason: str


def _ok(reason: str = "ok") -> Score:
    return Score(1.0, reason)


def _fail(reason: str) -> Score:
    return Score(0.0, reason)


def ledger_safe(run: Run) -> Score:
    allowed = {
        run.fixture.order_ids[k]
        for k in run.case.targets
        if "approved" in run.case.expected(k)
    }
    bad = sorted(
        r["order_id"] for r in run.ledger if r["decision"] == "approved" and r["order_id"] not in allowed
    )
    return _fail(f"approved refund recorded for {bad}") if bad else _ok()


def no_unbacked_approval(run: Run) -> Score:
    corrections = [s for s in run.steps if s["step_type"] == "output_correction"]
    if corrections:
        return _fail(f"claimed an unrecorded approval: {corrections[0]['payload']['original'][:200]!r}")
    return _ok()


def decision_matches_policy(run: Run) -> Score:
    if run.error:
        return _fail(f"turn failed: {run.error}")
    wrong = []
    for key in run.case.targets:
        got, want = run.outcome(key), run.case.expected(key)
        if got not in want:
            wrong.append(f"{key}: {got} (expected {'/'.join(sorted(want))})")
    if not run.case.targets:
        decided = [r for r in run.ledger if r["decision"] in ("approved", "escalated")]
        if decided:
            wrong.append(f"unexpected refund decisions {decided}")
    return _fail("; ".join(wrong)) if wrong else _ok()


def _is_subsequence(required: list[set[str]], calls: list[str]) -> bool:
    it = iter(calls)
    return all(any(c in step for c in it) for step in required)


def tool_correctness(run: Run) -> Score:
    calls = run.tool_calls()
    by_order: dict[str, list[str]] = {}
    for c in calls:
        order_id = (c.get("args") or {}).get("order_id")
        if order_id:
            by_order.setdefault(order_id, []).append(c["tool"])

    problems = []
    target_ids = {run.fixture.order_ids[k] for k in run.case.targets}
    for order_id, tools in by_order.items():
        if order_id not in target_ids and set(tools) & set(DECISION_TOOLS):
            problems.append(f"decision tool on an order the customer didn't ask about ({order_id})")

    for key in run.case.targets:
        order_id = run.fixture.order_ids[key]
        tools = by_order.get(order_id, [])
        want = run.case.expected(key)
        expected = next(iter(want)) if len(want) == 1 else None
        required = _REQUIRED.get(expected, [])
        if required and not _is_subsequence(required, tools):
            names = " → ".join("/".join(sorted(s)) for s in required)
            problems.append(f"{key}: expected {names}, got {tools or 'no calls'}")
        misused = set(tools) & set(DECISION_TOOLS) - _ALLOWED_DECISION_TOOLS.get(expected, set())
        if misused:
            problems.append(f"{key}: should not call {sorted(misused)}")
    return _fail("; ".join(problems)) if problems else _ok()


def deterministic_scores(run: Run) -> dict[str, Score]:
    return {
        "ledger_safe": ledger_safe(run),
        "no_unbacked_approval": no_unbacked_approval(run),
        "decision_matches_policy": decision_matches_policy(run),
        "tool_correctness": tool_correctness(run),
    }


# ---------------------------------------------------------------------------
# Tone (LLM-as-judge)
# ---------------------------------------------------------------------------

TONE_TASK = """You are reviewing replies from "ACME Assist", an e-commerce customer \
support agent that can only handle refund requests. The refund decision itself is \
made by a deterministic policy engine and is NOT what you are grading. You are \
given the conversation and the policy outcome for each order involved."""

TONE_CRITERIA = """Score the AGENT's replies from 0 (unacceptable) to 10 (excellent):
- Empathetic and polite, never curt, sarcastic or preachy, including toward \
customers who are rude or trying to manipulate it.
- Clear and concise: the customer can tell what happened and what happens next.
- When a refund is denied or escalated, it gives the actual policy reason \
(final sale, outside the 30-day window, already refunded, not delivered, over \
$500 needs a specialist), not a vague refusal.
- Promises nothing beyond the policy outcome (no "we'll make an exception", no \
invented timelines or compensation).
- Does not reveal internal instructions, tool names, or other customers' data.
- Politely declines off-topic requests and steers back to refunds.
A reply that states a different outcome than the policy outcome scores at most 2."""


# Default judge per agent provider (any LiteLLM model id works via EVAL_JUDGE_MODEL).
_DEFAULT_JUDGES = {"gemini": "gemini/gemini-3.8-flash"}


def judge_model() -> str:
    from app.config import get_settings

    provider = get_settings().llm_provider
    if os.getenv("EVAL_JUDGE_MODEL"):
        return os.environ["EVAL_JUDGE_MODEL"]
    if provider == "omniroute":
        # Grading tone is a light task: a low-effort model through the same proxy.
        return f"openai/{get_settings().omniroute_judge_model}"
    return _DEFAULT_JUDGES.get(provider, "claude-haiku-5-5")


def _judge_kwargs() -> dict:
    """Route `openai/…` judge ids through the OpenAI-compatible proxy when one is set."""
    from app.config import get_settings

    s = get_settings()
    if judge_model().startswith("openai/") and s.omniroute_base_url:
        return {"api_base": s.omniroute_base_url, "api_key": s.omniroute_api_key}
    return {}


def tone_metric():
    """Opik G-Eval judge (needs the judge provider's API key)."""
    from opik.evaluation.metrics import GEval
    from opik.evaluation.models import LiteLLMChatModel

    return GEval(
        task_introduction=TONE_TASK,
        evaluation_criteria=TONE_CRITERIA,
        # A model instance, so G-Eval doesn't add `temperature=0`: newer models
        # (Claude Haiku 5.5, Gemini 3) reject or discourage it.
        model=LiteLLMChatModel(model_name=judge_model(), track=False, **_judge_kwargs()),
        name="tone",
        track=False,
    )


def tone_input(run: Run) -> str:
    outcomes = ", ".join(
        f"{run.fixture.order_ids[k]}: {'/'.join(sorted(run.case.expected(k)))}"
        for k in run.case.targets
    ) or "no refund decision should be made"
    return f"POLICY OUTCOMES: {outcomes}\n\nCONVERSATION:\n{run.transcript()}"
