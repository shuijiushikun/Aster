"""评测样本的读取、分组隔离与本地知识依据校验。"""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
INTENT_PATH = DATA_DIR / "intent_cases.json"
RETRIEVAL_PATH = DATA_DIR / "retrieval_cases.json"
BUSINESS_PATH = DATA_DIR / "after_sales_cases.json"
Split = Literal["development", "calibration", "evaluation"]
Label = Literal["query", "refund", "exchange", "clarify"]


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    group: str = Field(min_length=1)
    split: Split
    rationale: str = Field(min_length=1)
    origin: Literal["authored", "legacy_smoke", "legacy_retrieval"]


class DialogueTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class IntentCase(Sample):
    category: str = Field(min_length=1)
    history: list[DialogueTurn] = Field(default_factory=list)
    text: str = Field(min_length=1)
    expected_intent: Label


class RetrievalCase(Sample):
    query: str = Field(min_length=1, max_length=2000)
    model_code: str | None
    category: Literal["digital", "apparel"] | None
    expected_document_id: str | None
    expected_text: str | None = None
    negative_kind: Literal["missing_fact", "unknown_model", "scope_mismatch", "off_topic"] | None = None

    @model_validator(mode="after")
    def evidence_label(self):
        if self.expected_document_id:
            if not self.expected_text or self.negative_kind:
                raise ValueError("有答案题必须标注依据片段，且不能标为负例")
        elif not self.negative_kind or self.expected_text:
            raise ValueError("无答案题必须说明负例类型，且不能虚构依据")
        return self


class BusinessStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: Literal["customer_a", "customer_b", "admin", "system"]
    action: Literal["chat", "supplement_ticket", "decide_ticket", "cancel_ticket",
                    "resume_ticket", "restart_worker", "replay_request"]
    input: str = Field(min_length=1)
    expected_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)


class BusinessExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_refunded: bool | None = None
    approved_refunds_max: int = Field(default=0, ge=0, le=1)
    active_tickets_max: int = Field(default=1, ge=0, le=1)
    acceptances_max: int = Field(default=0, ge=0, le=1)
    ticket_status_in: list[str] = Field(default_factory=list)
    allow_no_ticket: bool = False
    checks: list[str] = Field(min_length=1)


class BusinessCase(Sample):
    title: str = Field(min_length=1)
    suite: Literal["current", "target"] = "current"
    execution: Literal["scenario_spec"] = "scenario_spec"
    known_gap: str | None = None
    setup: dict = Field(min_length=1)
    steps: list[BusinessStep] = Field(min_length=1)
    expected: BusinessExpectation

    @model_validator(mode="after")
    def target_requires_gap(self):
        if (self.suite == "target") != bool(self.known_gap):
            raise ValueError("目标场景必须说明尚未实现的机制；当前场景不填写 known_gap")
        return self


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(case) -> str:
    if isinstance(case, IntentCase):
        payload = [case.text, [turn.model_dump() for turn in case.history]]
    elif isinstance(case, RetrievalCase):
        payload = [case.query, case.model_code, case.category]
    else:
        payload = [case.setup, [step.model_dump() for step in case.steps]]
    return re.sub(r"\s+", "", json.dumps(payload, ensure_ascii=False, sort_keys=True)).casefold()


def validate_groups(cases) -> None:
    ids, groups, signatures = set(), {}, set()
    for case in cases:
        if case.id in ids:
            raise ValueError(f"重复样本 ID：{case.id}")
        ids.add(case.id)
        previous = groups.setdefault(case.group, case.split)
        if previous != case.split:
            raise ValueError(f"分组跨集合：{case.group}")
        signature = _signature(case)
        if signature in signatures:
            raise ValueError(f"重复输入：{case.id}")
        signatures.add(signature)


def _load(path, model):
    cases = [model.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8"))]
    if not cases:
        raise ValueError(f"空数据集：{path.name}")
    validate_groups(cases)
    return cases


def load_intent_cases(path: Path = INTENT_PATH) -> list[IntentCase]:
    return _load(path, IntentCase)


def load_retrieval_cases(path: Path = RETRIEVAL_PATH) -> list[RetrievalCase]:
    return _load(path, RetrievalCase)


def load_business_cases(path: Path = BUSINESS_PATH) -> list[BusinessCase]:
    return _load(path, BusinessCase)


def check_retrieval_evidence(cases: list[RetrievalCase]) -> None:
    from app.rag.corpus import load_corpus

    documents, nodes, _ = load_corpus()
    catalog = {doc.document_id: doc for doc in documents}
    for case in cases:
        if not case.expected_document_id:
            continue
        doc = catalog.get(case.expected_document_id)
        if doc is None:
            raise ValueError(f"知识依据文档不存在：{case.id}")
        if doc.model_code and (doc.model_code != case.model_code or doc.category != case.category):
            raise ValueError(f"知识依据范围不符：{case.id}")
        if not any(n.metadata["document_id"] == doc.document_id and case.expected_text in n.text for n in nodes):
            raise ValueError(f"知识依据片段不存在：{case.id}")


def main():
    intent, retrieval, business = load_intent_cases(), load_retrieval_cases(), load_business_cases()
    check_retrieval_evidence(retrieval)
    report = {}
    for name, cases, path in [("intent", intent, INTENT_PATH), ("retrieval", retrieval, RETRIEVAL_PATH),
                              ("business", business, BUSINESS_PATH)]:
        report[name] = {"total": len(cases), "groups": len({c.group for c in cases}),
                        "splits": dict(Counter(c.split for c in cases)), "sha256": fingerprint(path)}
    report["intent"]["labels"] = dict(Counter(c.expected_intent for c in intent))
    report["retrieval"]["negative_kinds"] = dict(Counter(c.negative_kind for c in retrieval if c.negative_kind))
    report["business"]["suites"] = dict(Counter(c.suite for c in business))
    report["business"]["execution"] = "场景规范，未在此命令中回放对话"
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
