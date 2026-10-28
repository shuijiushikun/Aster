"""真实检索加最终知识回答评测；不连接业务库，不执行写入工具。"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.agent.answer_grounding import ground_answer
from app.agent.llm import get_chat_model, get_fallback_model
from app.agent.runtime import ModelCalls, TurnRuntime
from app.config import get_settings
from app.rag.corpus import load_corpus
from app.rag.retriever import KnowledgeRetriever
from app.rag.store import open_store
from evals.datasets import RETRIEVAL_PATH, fingerprint, load_retrieval_cases


def score_answers(rows):
    positive = [r for r in rows if r["expected"]]
    negative = [r for r in rows if not r["expected"]]
    return {"total": len(rows), "passed": sum(r["passed"] for r in rows),
            "positive": len(positive), "positive_answered": sum(r["passed"] for r in positive),
            "negative": len(negative), "negative_abstained": sum(r["passed"] for r in negative),
            "negative_answered": sum(r["answer_status"] in ("supported", "partial") for r in negative),
            "verification_unavailable": sum(r["answer_status"] == "unavailable" for r in rows),
            "retrieval_unavailable": sum(r["retrieval_status"] == "unavailable" for r in rows)}


async def evaluate(*, split="evaluation", limit=None, concurrency=3, case_ids=None):
    implementation_sha = fingerprint(Path(__file__).resolve().parents[1] / "app/agent/answer_grounding.py")
    settings = get_settings()
    cases = [case for case in load_retrieval_cases() if case.split == split]
    if case_ids:
        cases = [case for case in cases if case.id in case_ids]
        if set(case_ids) != {case.id for case in cases}:
            raise ValueError("指定题号不属于所选数据分组")
    if limit is not None:
        cases = cases[:limit]
    runtime = TurnRuntime()
    caller = ModelCalls(get_chat_model(), get_fallback_model())
    _, _, generation = load_corpus()
    semaphore = asyncio.Semaphore(concurrency)

    class Recorder:
        def __init__(self):
            self.responses = []

        async def call(self, *args, **kwargs):
            result = await caller.call(*args, **kwargs)
            self.responses.append({"content": result.content, "finish_reason": result.response_metadata.get("finish_reason")})
            return result
    async with open_store(settings) as (store, client):
        retriever = KnowledgeRetriever(store, client, settings)
        vectors = await retriever.embedding.aget_text_embedding_batch([c.query for c in cases])

        async def run(case, vector):
            async with semaphore:
                started = time.perf_counter()
                evidence = await retriever.retrieve(case.query, model_code=case.model_code,
                    category=case.category, mode="hybrid", rerank=True, query_embedding=vector)
                if evidence.status == "unavailable":
                    status, text, claims, sources = "unavailable", "检索服务不可用", [], []
                else:
                    recorder = Recorder()
                    answer = await ground_answer(case.query, evidence.model_dump(), recorder,
                                                {"configurable": {"turn_runtime": runtime}})
                    status, text, claims, sources = answer.status, answer.text, answer.claims, answer.sources
                expected = case.expected_document_id
                # 判断是否回答/拒答，以及采用的依据范围；不将其称作整段答案的人工正确率。
                passed = (status in ("supported", "partial") and any(c["document_id"] == expected for c in claims)
                          if expected else status == "insufficient")
                row = {"id": case.id, "group": case.group, "expected": expected, "passed": passed,
                       "query": case.query, "retrieval_status": evidence.status, "answer_status": status,
                       "answer": text, "claims": claims,
                       "retrieved": [{"document_id": s.document_id, "chunk_index": s.chunk_index} for s in evidence.sources],
                       "used_sources": [{k: s[k] for k in ("document_id", "revision", "chunk_index")} for s in sources],
                       "elapsed_ms": round((time.perf_counter() - started) * 1000)}
                if evidence.status != "unavailable" and status == "unavailable":
                    row.update(verification_reason=answer.reason, diagnostic_responses=recorder.responses)
                print(json.dumps({k: row[k] for k in ("id", "passed", "retrieval_status", "answer_status")}), flush=True)
                return row

        rows = await asyncio.gather(*(run(c, v) for c, v in zip(cases, vectors, strict=True)))
    return {"split": split, "limit": limit, "concurrency": concurrency,
            "dataset_sha256": fingerprint(RETRIEVAL_PATH), "generation": generation,
            "implementation_sha256": implementation_sha,
            "mode": "hybrid+rerank", "min_dense_score": settings.rag_min_dense_score,
            "min_bm25_score": settings.rag_min_bm25_score,
            "summary": score_answers(rows), "usage_calls": len(runtime.usage),
            "usage_unknown": sum(u["status"] != "reported" for u in runtime.usage),
            "tokens": sum((u["input_tokens"] or 0) + (u["output_tokens"] or 0) for u in runtime.usage),
            "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--split", choices=["development", "calibration", "evaluation"], default="evaluation")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args()
    if not args.live:
        parser.error("需要 --live，使用已配置的检索和生成服务")
    if not 1 <= args.concurrency <= 8 or (args.limit is not None and args.limit < 1):
        parser.error("并发应为1到8，limit应为正整数")
    report = asyncio.run(evaluate(split=args.split, limit=args.limit, concurrency=args.concurrency, case_ids=args.case_ids))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return int(report["summary"]["passed"] != report["summary"]["total"])


if __name__ == "__main__":
    raise SystemExit(main())
