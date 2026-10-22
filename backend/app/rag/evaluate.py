"""显式真实检索评测；不调用生成模型，不创建/清空测试业务库。"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.config import BACKEND_ROOT, get_settings
from app.rag.corpus import load_corpus
from app.rag.retriever import KnowledgeRetriever
from app.rag.store import open_store

CASES = BACKEND_ROOT / "data" / "retrieval_cases.json"


async def evaluate(*, calibrate=False) -> dict:
    settings = get_settings()
    if calibrate:
        settings = settings.model_copy(update={"rag_min_dense_score": 0.0})
    cases = [c for c in json.loads(CASES.read_text(encoding="utf-8"))
             if c["split"] == ("calibration" if calibrate else "evaluation")]
    _, nodes, generation = load_corpus()
    catalog = {(n.metadata["document_id"], n.metadata["revision"], n.metadata["chunk_index"]): n for n in nodes}
    modes = [("dense", False)] if calibrate else [("dense", False), ("hybrid", False), ("hybrid", True)]
    rows = []
    async with open_store(settings) as (store, client):
        retriever = KnowledgeRetriever(store, client, settings)
        # 各模式共享同一批真实查询向量；避免比较时重复计费。
        embeddings = await retriever.embedding.aget_text_embedding_batch([c["query"] for c in cases])
        for case, vector in zip(cases, embeddings, strict=True):
            for mode, rerank in modes:
                start = time.perf_counter()
                result = await retriever.retrieve(case["query"], model_code=case["model_code"],
                    category=case["category"], mode=mode, rerank=rerank, query_embedding=vector)
                expected = case["expected_document_id"]
                matches = [s.document_id == expected and case.get("expected_text", "") in s.excerpt
                           for s in result.sources]
                valid = sum((s.document_id, s.revision, s.chunk_index) in catalog and
                            catalog[(s.document_id, s.revision, s.chunk_index)].text.startswith(s.excerpt)
                            for s in result.sources)
                cross = sum(s.model_code is not None and (s.model_code != case["model_code"] or
                                                         s.category != case["category"]) for s in result.sources)
                rows.append(dict(id=case["id"], mode=mode + ("+rerank" if rerank else ""),
                    expected=expected, status=result.status, reason=result.reason, warnings=result.warnings,
                    hit_at_1=bool(matches and matches[0]), hit_at_4=any(matches),
                    score=result.sources[0].score if result.sources else 0,
                    valid_citations=valid, citations=len(result.sources), cross_model=cross,
                    elapsed_ms=round((time.perf_counter()-start)*1000),
                    sources=[dict(document_id=s.document_id, chunk_index=s.chunk_index) for s in result.sources]))
    report = dict(embedding_model=settings.embed_model_name, dimension=settings.embed_dimension,
                  rerank_model=settings.rerank_model, generation=generation,
                  min_dense_score=settings.rag_min_dense_score, split="calibration" if calibrate else "evaluation")
    if calibrate:
        if any(r["status"] == "unavailable" for r in rows):
            raise RuntimeError("calibration dependency unavailable")
        if any(r["expected"] and r["status"] != "ok" for r in rows):
            raise RuntimeError("calibration positive cases have no candidates; check index before calibrating")
        scores = sorted({r["score"] for r in rows})
        thresholds = [0.0, 1.0] + [(a+b)/2 for a, b in zip(scores, scores[1:], strict=False)]
        def quality(threshold):
            correct = sum((r["score"] >= threshold) == bool(r["expected"]) for r in rows)
            false_positive = sum(r["score"] >= threshold and not r["expected"] for r in rows)
            return correct, -false_positive
        threshold = max(thresholds, key=quality)
        report.update(suggested_min_dense_score=round(threshold, 6),
                      calibration_correct=quality(threshold)[0], cases=len(rows), rows=rows)
        return report
    summaries = {}
    for mode in {r["mode"] for r in rows}:
        subset = [r for r in rows if r["mode"] == mode]
        positives = [r for r in subset if r["expected"]]
        negatives = [r for r in subset if not r["expected"]]
        citations = sum(r["citations"] for r in subset)
        summaries[mode] = dict(
            positive_cases=len(positives), negative_cases=len(negatives),
            hit_at_1=sum(r["hit_at_1"] for r in positives)/len(positives),
            hit_at_4=sum(r["hit_at_4"] for r in positives)/len(positives),
            abstention_correct=sum(r["status"] == "insufficient_evidence" for r in negatives),
            citation_validity=sum(r["valid_citations"] for r in subset)/citations if citations else 0,
            cross_model=sum(r["cross_model"] for r in subset),
            errors=sum(r["status"] == "unavailable" for r in subset),
            rerank_fallbacks=sum(bool(r["warnings"]) for r in subset),
            mean_retrieval_ms=round(sum(r["elapsed_ms"] for r in subset)/len(subset)),
        )
    report.update(summary=summaries, rows=rows, note="Small hand-labelled demonstration set; latency excludes shared query embedding batch.")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly call configured Qdrant and Bailian")
    parser.add_argument("--calibrate", action="store_true", help="use separate calibration cases, no env mutation")
    parser.add_argument("--out", type=Path, default=BACKEND_ROOT / "var" / "retrieval-evaluation.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; real embedding/rerank requests consume quota")
    report = asyncio.run(evaluate(calibrate=args.calibrate))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))
    if not args.calibrate and any(s["errors"] or s["cross_model"] or s["citation_validity"] < 1
                                  for s in report["summary"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
