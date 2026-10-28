"""真实本地意图评测；默认仍运行原 32 条冒烟题，不调用业务工具。"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from app.after_sales.intent import classify_intent
from app.agent.runtime import TurnRuntime
from app.config import get_settings
from evals.datasets import INTENT_PATH, fingerprint, load_intent_cases
from evals.intent_metrics import score_intents


def select_cases(split: str, *, path: Path | None = None):
    cases = load_intent_cases(path) if path else load_intent_cases()
    if split == "smoke":
        return [c for c in cases if c.origin == "legacy_smoke"]
    return cases if split == "all" else [c for c in cases if c.split == split]


async def smoke(split="smoke", *, limit=None, out: Path | None = None, dataset: Path | None = None):
    settings = get_settings()
    runtime = TurnRuntime()
    rows = []
    cases = select_cases(split, path=dataset) if dataset else select_cases(split)
    if limit is not None:
        cases = cases[:limit]
    for case in cases:
        history = [(HumanMessage if turn.role == "user" else AIMessage)(turn.content)
                   for turn in case.history]
        start = time.perf_counter()
        result = await classify_intent([*history, HumanMessage(case.text)],
                                      {"configurable": {"turn_runtime": runtime}})
        row = {"id": case.id, "group": case.group, "split": case.split, "case": case.category,
               "expected": case.expected_intent, "actual": result.intent,
               "source": result.source, "fallback_reason": result.fallback_reason,
               "model_intent": result.model_intent, "guard_reason": result.guard_reason,
               "seconds": round(time.perf_counter() - start, 2),
               "passed": result.source == "model" and result.intent == case.expected_intent}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    passed = sum(row["passed"] for row in rows)
    report = {"model": settings.intent_classifier_model, "split": split, "limit": limit,
              "dataset_sha256": fingerprint(dataset or INTENT_PATH),
              "implementation_sha256": fingerprint(Path(__file__).resolve().parents[1] / "app/after_sales/intent.py"),
              "passed": passed, "total": len(rows), "scores": score_intents(rows),
              "reported_tokens": sum((u["input_tokens"] or 0) + (u["output_tokens"] or 0)
                                     for u in runtime.usage),
              "unreported_usage_calls": sum(u["status"] != "reported" for u in runtime.usage)}
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report | {"rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if passed == len(rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="允许真实调用已配置的本地 Ollama")
    parser.add_argument("--split", choices=["smoke", "development", "evaluation", "all"], default="smoke")
    parser.add_argument("--limit", type=int, help="仅运行前 N 条；报告会记录此限制")
    parser.add_argument("--out", type=Path, help="将指标和逐条结果写入 JSON")
    parser.add_argument("--dataset", type=Path, help="使用另行冻结的新增样本文件")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实分类验证需要 --live；只使用虚构文本，不访问数据库")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须是正整数")
    return asyncio.run(smoke(args.split, limit=args.limit, out=args.out, dataset=args.dataset))


if __name__ == "__main__":
    raise SystemExit(main())
