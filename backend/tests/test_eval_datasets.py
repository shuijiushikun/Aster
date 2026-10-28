"""数据约束与评测指标；不调用模型、向量库或业务数据库。"""

import pytest
from pydantic import ValidationError

from evals.datasets import (
    IntentCase,
    RetrievalCase,
    check_retrieval_evidence,
    load_business_cases,
    load_intent_cases,
    load_retrieval_cases,
    validate_groups,
)
from evals.intent_metrics import score_intents


def intent_case(**changes):
    values = dict(id="I1", group="g1", split="development", category="policy",
                  history=[], text="这笔订单能退款吗", expected_intent="query",
                  rationale="只查询资格。", origin="authored")
    return IntentCase(**(values | changes))


def test_dataset_sizes_and_original_smoke_are_preserved():
    intents = load_intent_cases()
    retrieval = load_retrieval_cases()
    business = load_business_cases()
    assert 240 <= len(intents) <= 300
    assert 180 <= len(retrieval) <= 240
    assert 30 <= len(business) <= 50
    original = [c for c in intents if c.origin == "legacy_smoke"]
    assert len(original) == 32
    assert {c.split for c in original} == {"development"}
    assert {c.expected_intent for c in intents if c.split == "evaluation"} == {
        "query", "refund", "exchange", "clarify",
    }
    assert {c.split for c in retrieval} == {"development", "calibration", "evaluation"}
    check_retrieval_evidence(retrieval)


def test_related_paraphrases_cannot_cross_splits():
    first = intent_case()
    second = intent_case(id="I2", split="evaluation", text="可以先看看退款资格吗")
    with pytest.raises(ValueError, match="分组"):
        validate_groups([first, second])


def test_duplicate_dialogue_is_rejected_even_with_a_new_group():
    first = intent_case()
    second = intent_case(id="I2", group="g2", split="evaluation")
    with pytest.raises(ValueError, match="重复"):
        validate_groups([first, second])


def test_same_short_reply_with_different_history_is_not_a_duplicate():
    first = intent_case(text="好的", history=[{"role": "assistant", "content": "查资格吗？"}])
    second = intent_case(id="I2", group="g2", text="好的", expected_intent="refund",
                         history=[{"role": "assistant", "content": "确认提交退款吗？"}])
    validate_groups([first, second])


def test_unknown_intent_label_is_rejected():
    with pytest.raises(ValidationError):
        intent_case(expected_intent="admin")


def test_positive_retrieval_requires_actual_supporting_text():
    case = RetrievalCase(id="R1", group="r1", split="evaluation", query="M码胸宽多少",
        model_code="TSHIRT-01", category="apparel", expected_document_id="TS01-SIZE",
        expected_text="凭空编造的尺寸999厘米", rationale="核对尺寸", origin="authored")
    with pytest.raises(ValueError, match="依据"):
        check_retrieval_evidence([case])


def test_positive_retrieval_cannot_cite_another_product():
    case = RetrievalCase(id="R1", group="r1", split="evaluation", query="耳机尺码多少",
        model_code="HP-01", category="digital", expected_document_id="TS01-SIZE",
        expected_text="| M | 51 | 68 | 44 |", rationale="核对范围", origin="authored")
    with pytest.raises(ValueError, match="范围"):
        check_retrieval_evidence([case])


def test_negative_retrieval_requires_reason_instead_of_a_fake_answer():
    with pytest.raises(ValidationError):
        RetrievalCase(id="R1", group="r1", split="evaluation", query="芯片焊点电压",
            model_code="HP-01", category="digital", expected_document_id=None,
            rationale="资料缺失", origin="authored")


def test_metrics_separate_fallbacks_and_dangerous_write_predictions():
    rows = [
        {"expected": "query", "actual": "query", "source": "model"},
        {"expected": "query", "actual": "refund", "source": "model"},
        {"expected": "refund", "actual": "query", "source": "model"},
        {"expected": "exchange", "actual": "exchange", "source": "regex"},
    ]
    scores = score_intents(rows)
    assert scores["label_accuracy"] == 0.5
    assert scores["model_only_accuracy"] == pytest.approx(1 / 3)
    assert scores["model_coverage"] == 0.75
    assert scores["fallback_count"] == 1
    assert scores["unsafe_write_count"] == 1
    assert scores["unsafe_write_rate"] == 0.5
    assert scores["confusion_matrix"]["refund"]["query"] == 1


def test_no_model_output_is_not_reported_as_perfect_model_accuracy():
    scores = score_intents([{"expected": "query", "actual": "query", "source": "regex"}])
    assert scores["model_only_accuracy"] is None
    assert scores["model_coverage"] == 0


def test_guarded_prediction_does_not_inflate_raw_model_accuracy():
    scores = score_intents([{"expected": "clarify", "actual": "clarify", "model_intent": "refund",
                            "source": "model", "guard_reason": "ambiguous_confirmation"}])
    assert scores["label_accuracy"] == 1
    assert scores["model_only_accuracy"] == 0
    assert scores["unsafe_write_count"] == 0 and scores["raw_model_unsafe_write_count"] == 1
    assert scores["guard_count"] == 1


def test_smoke_selection_is_backward_compatible_and_test_set_is_separate():
    from scripts.intent_smoke import select_cases

    smoke = select_cases("smoke")
    evaluation = select_cases("evaluation")
    assert len(smoke) == 32
    assert len(evaluation) == 168
    assert not {c.group for c in smoke} & {c.group for c in evaluation}


def test_prompt_examples_are_not_exact_test_inputs():
    from app.after_sales.intent import _EXAMPLES

    examples = {(s["current"], tuple((m["role"], m["content"]) for m in s["history"]))
                for s, _ in _EXAMPLES}
    for case in load_intent_cases():
        if case.split == "evaluation":
            assert (case.text, tuple((m.role, m.content) for m in case.history)) not in examples


def test_new_holdout_is_balanced_and_has_no_duplicate_dialogue_with_existing_cases():
    from evals.datasets import DATA_DIR

    cases = load_intent_cases(DATA_DIR / "intent_holdout_cases.json")
    assert len(cases) == 48
    assert all(c.split == "evaluation" for c in cases)
    assert {label: sum(c.expected_intent == label for c in cases) for label in
            ("query", "refund", "exchange", "clarify")} == dict.fromkeys(("query", "refund", "exchange", "clarify"), 12)
    validate_groups([*load_intent_cases(), *cases])


def test_business_specification_distinguishes_current_capabilities_and_targets():
    from app.mcp.tools import build_after_sales_tools

    tools = {tool.name for tool in build_after_sales_tools()}
    cases = load_business_cases()
    assert sum(case.suite == "target" for case in cases) == 4
    for case in cases:
        assert case.execution == "scenario_spec"
        assert bool(case.known_gap) == (case.suite == "target")
        for step in case.steps:
            assert set(step.expected_tools + step.forbidden_tools) <= tools


async def test_intent_runner_persists_dataset_identity_and_actual_scores(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    from app.after_sales.intent import TurnIntent, message_digest
    from scripts import intent_smoke

    case = intent_case(split="evaluation")

    async def classify(messages, config):
        return TurnIntent(intent="query", source="model", message_sha256=message_digest(case.text))

    monkeypatch.setattr(intent_smoke, "select_cases", lambda split: [case])
    monkeypatch.setattr(intent_smoke, "classify_intent", classify)
    monkeypatch.setattr(intent_smoke, "get_settings", lambda: SimpleNamespace(intent_classifier_model="test-only"))
    output = tmp_path / "intent.json"
    assert await intent_smoke.smoke("evaluation", out=output) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["total"] == 1
    assert report["scores"]["label_accuracy"] == 1
    assert len(report["dataset_sha256"]) == 64
    assert report["rows"][0]["id"] == case.id
