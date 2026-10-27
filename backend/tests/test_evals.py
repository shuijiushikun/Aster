"""The eval harness (`backend/evals`): case files, outcome extraction, metrics
and the CI gate. The golden set itself runs via `python -m evals.run`."""

from __future__ import annotations

import string

import pytest
from langchain_core.messages import AIMessage

from app.agent import graph as graph_module
from app.agent.llm_fake import ScriptedFakeChatModel
from evals import report
from evals.harness import BRANCHES, OTHER_KEY, Case, Fixture, Run, load_cases, run_case
from evals.metrics import deterministic_scores
from tests.test_resilience import FakeModel, _tool_call

KEYS = [*BRANCHES, OTHER_KEY]


def test_eval_environment_isolates_legacy_refund_suite(monkeypatch, tmp_path):
    from evals.run import _prepare_env

    monkeypatch.setenv("AGENT_V2_ENABLED", "true")
    monkeypatch.setenv("INTENT_CLASSIFIER_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///not-the-eval-database.db")
    monkeypatch.setenv("REDIS_URL", "redis://not-the-test-instance:6379/0")
    monkeypatch.setenv("SEED_ON_STARTUP", "true")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("FAKE_LLM_LATENCY_MS", "100")
    monkeypatch.setenv("FAKE_LLM_ERROR_RATE", "1")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "openai")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("OPIK_TRACK_DISABLE", "true")
    _prepare_env("scripted", str(tmp_path))

    import os

    assert os.environ["DATABASE_URL"] == f"sqlite:///{tmp_path}/evals.db"
    assert os.environ["REDIS_URL"] == ""
    assert os.environ["AGENT_V2_ENABLED"] == "false"
    assert os.environ["INTENT_CLASSIFIER_ENABLED"] == "false"
    assert os.environ["LLM_PROVIDER"] == "fake"


def test_case_files_are_valid():
    cases = load_cases()
    assert cases
    assert set(BRANCHES) <= {target for case in cases for target in case.targets}
    fixture = Fixture("EV-001", "EVX-001", {k: f"EV-001-{k}" for k in KEYS})
    for case in cases:
        for text in case.turns:
            fixture.render(text)  # every placeholder resolves
            fields = {f for _, f, _, _ in string.Formatter().parse(text) if f}
            assert fields <= set(KEYS), case.id
        # A case's targets are the orders its text mentions.
        mentioned = {f for t in case.turns for _, f, _, _ in string.Formatter().parse(t) if f}
        assert mentioned <= set(case.targets) or case.live_only, case.id
    tags = {t for c in cases for t in c.tags}
    assert {"edge", "multi_turn", "chinese"} <= tags


@pytest.fixture
def scripted(monkeypatch):
    monkeypatch.setattr(graph_module, "get_chat_model", lambda: ScriptedFakeChatModel(latency_ms=0))


def _case(id_, turns, targets, **kw) -> Case:
    return Case(id=id_, turns=turns, targets=targets, tags=["test"], **kw)


@pytest.mark.parametrize(
    "turns,targets,outcomes",
    [
        (["refund {A}"], ["A"], {"A": "approved"}),
        (["refund {C}"], ["C"], {"C": "escalated"}),
        (["refund {G}"], ["G"], {"G": "approved"}),  # exactly $500
        (["refund {H}"], ["H"], {"H": "approved"}),  # day 30
        (["refund {I}"], ["I"], {"I": "denied"}),  # day 31
        (["refund {J}"], ["J"], {"J": "denied"}),  # final sale beats escalation
        (["refund {F}"], ["F"], {"F": "denied"}),  # not delivered
        (["refund {X}"], ["X"], {"X": "refused"}),
        (["refund {B}", "and refund {A}"], ["A", "B"], {"A": "approved", "B": "denied"}),
    ],
)
async def test_scripted_runs_reach_policy_outcomes(engine, scripted, turns, targets, outcomes):
    run = await run_case(_case("t", turns, targets))
    assert {k: run.outcome(k) for k in targets} == outcomes
    assert all(s.value == 1 for s in deterministic_scores(run).values()), deterministic_scores(run)


async def test_compromised_model_fails_the_right_metrics(engine, monkeypatch):
    # Jumps straight to issue_refund on a final-sale order and claims success.
    order = {}

    def model():
        return FakeModel(
            [
                AIMessage(content="", tool_calls=[_tool_call("issue_refund", {"order_id": order["B"]}, "1")]),
                AIMessage(content="Your refund has been approved!"),
            ]
        )

    case = _case("t", ["refund {B}"], ["B"])
    real_create = __import__("evals.harness", fromlist=["create_fixture"]).create_fixture

    async def create_fixture():
        fx = await real_create()
        order.update(fx.order_ids)
        return fx

    monkeypatch.setattr("evals.harness.create_fixture", create_fixture)
    monkeypatch.setattr(graph_module, "get_chat_model", model)
    run = await run_case(case)
    scores = deterministic_scores(run)
    assert scores["ledger_safe"].value == 1  # the gate still denied it
    assert run.outcome("B") == "denied"
    assert scores["no_unbacked_approval"].value == 0
    assert scores["tool_correctness"].value == 0
    assert "issue_refund" in scores["tool_correctness"].reason


def _run(ledger, targets=("B",), steps=()) -> Run:
    fx = Fixture("EV-1", "EVX-1", {k: f"EV-1-{k}" for k in KEYS})
    turns = [{"user": "u", "assistant": "a", "events": list(steps)}]
    return Run(_case("t", ["x"], list(targets)), fx, turns, ledger)


def test_ledger_safe_flags_any_unexpected_approval():
    assert deterministic_scores(_run([]))["ledger_safe"].value == 1
    bad = _run([{"order_id": "EV-1-B", "decision": "approved"}])
    assert deterministic_scores(bad)["ledger_safe"].value == 0
    other = _run([{"order_id": "EVX-1-A", "decision": "approved"}], targets=("X",))
    assert deterministic_scores(other)["ledger_safe"].value == 0


def test_decision_tool_on_an_unrelated_order_is_a_tool_error():
    call = {"kind": "step", "step_type": "tool_call", "payload": {"tool": "issue_refund", "args": {"order_id": "EV-1-D"}}}
    scores = deterministic_scores(_run([], targets=("A",), steps=[call]))
    assert scores["tool_correctness"].value == 0
    assert "didn't ask about" in scores["tool_correctness"].reason


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _results(per_case: list[dict[str, float]], mode="scripted", tone=None) -> dict:
    cases = []
    for i, values in enumerate(per_case):
        scores = {m: {"value": v, "reason": "r"} for m, v in values.items()}
        if tone is not None:
            scores["tone"] = {"value": tone[i], "reason": "r"}
        cases.append({"id": f"c{i}", "tags": ["edge"], "scores": scores, "retried": False})
    return {"mode": mode, "cases": cases}


GOOD = {"ledger_safe": 1, "no_unbacked_approval": 1, "decision_matches_policy": 1, "tool_correctness": 1}


def _gate(results, baseline=None):
    return report.gate(results, report.summarise(results), baseline)


def test_gate_floors_fail_on_a_single_case():
    results = _results([GOOD] * 99 + [GOOD | {"ledger_safe": 0}])
    assert any("ledger_safe" in f for f in _gate(results))
    results = _results([GOOD] * 99 + [GOOD | {"no_unbacked_approval": 0}])
    assert any("no_unbacked_approval" in f for f in _gate(results))


def test_gate_compares_with_the_baseline():
    baseline = report.summarise(_results([GOOD] * 50))
    one_off = _results([GOOD] * 49 + [GOOD | {"tool_correctness": 0}])  # -2.0 pts: within tolerance
    assert _gate(one_off, baseline) == []
    two_off = _results([GOOD] * 48 + [GOOD | {"decision_matches_policy": 0}] * 2)  # -4 pts
    assert any("decision_matches_policy` regressed" in f for f in _gate(two_off, baseline))
    # Without a baseline only the floors apply.
    assert _gate(two_off, None) == []


def test_gate_checks_tone_and_judge_failures():
    baseline = report.summarise(_results([GOOD] * 4, mode="live", tone=[0.8] * 4))
    worse = _results([GOOD] * 4, mode="live", tone=[0.7] * 4)
    assert any("tone` regressed" in f for f in _gate(worse, baseline))
    flaky_judge = _results([GOOD] * 4, mode="live", tone=[0.8, 0.8, None, None])
    assert any("judge failed" in f for f in _gate(flaky_judge, baseline))


def test_gate_reports_a_provider_outage_not_a_regression():
    # Every turn errored (the model was unreachable): say so, don't blame quality.
    baseline = report.summarise(_results([GOOD] * 10))
    down = _results([GOOD | {"decision_matches_policy": 0, "tool_correctness": 0}] * 10)
    for c in down["cases"]:
        c["error"] = "Sorry, something went wrong on our side."
    failures = _gate(down, baseline)
    assert len(failures) == 1 and "unreachable" in failures[0]
    # A money-safety failure is still reported during an outage.
    down["cases"][0]["scores"]["ledger_safe"]["value"] = 0
    assert any("ledger_safe" in f for f in _gate(down, baseline))
