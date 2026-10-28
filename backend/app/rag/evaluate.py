"""显式真实检索评测；不调用生成模型，不创建/清空测试业务库。"""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

from llama_index.core.schema import QueryBundle

from app.config import BACKEND_ROOT, get_settings
from app.rag.corpus import load_corpus
from app.rag.retriever import KnowledgeRetriever, scope_filter
from app.rag.sparse import BM25_B, BM25_K1, SPARSE_VERSION
from app.rag.store import open_store, read_manifest
from evals.datasets import RETRIEVAL_PATH, fingerprint, load_retrieval_cases


def select_threshold(rows, field, *, upper=None):
    scores = sorted({float(row[field]) for row in rows})
    thresholds = [0.0, upper if upper is not None else math.nextafter(scores[-1], math.inf)]
    thresholds.extend((a + b) / 2 for a, b in zip(scores, scores[1:], strict=False))

    def quality(threshold):
        correct = sum((row[field] >= threshold) == bool(row["expected"]) for row in rows)
        false_positive = sum(row[field] >= threshold and not row["expected"] for row in rows)
        return correct, -false_positive, threshold

    threshold = max(thresholds, key=quality)
    return threshold, quality(threshold)[0]


def calibrated_settings(settings, path: Path, generation: str):
    report = json.loads(path.read_text(encoding="utf-8"))
    if (report.get("split") != "calibration" or report.get("dataset_sha256") != fingerprint(RETRIEVAL_PATH)
            or report.get("generation") != generation or report.get("embedding_model") != settings.embed_model_name
            or report.get("dimension") != settings.embed_dimension or report.get("sparse_version") != SPARSE_VERSION):
        raise ValueError("校准报告的数据、知识版本或检索配置与当前运行不一致")
    dense = float(report["suggested_min_dense_score"])
    bm25 = float(report["suggested_min_bm25_score"])
    if not math.isfinite(dense) or not 0 <= dense <= 1 or not math.isfinite(bm25) or bm25 < 0:
        raise ValueError("校准报告包含无效阈值")
    return settings.model_copy(update={"rag_min_dense_score": dense, "rag_min_bm25_score": bm25})


async def evaluate(*, calibrate=False, calibration_path: Path | None = None, concurrency=1) -> dict:
    if not 1 <= concurrency <= 8:
        raise ValueError("评测并发数应在 1 到 8 之间")
    settings = get_settings()
    if calibrate and calibration_path:
        raise ValueError("校准和读取既有校准报告应分别运行")
    if calibrate:
        settings = settings.model_copy(update={"rag_min_dense_score": 0.0})
    split = "calibration" if calibrate else "evaluation"
    cases = [c.model_dump() for c in load_retrieval_cases() if c.split == split]
    _, nodes, generation = load_corpus()
    if calibration_path:
        settings = calibrated_settings(settings, calibration_path, generation)
    catalog = {(n.metadata["document_id"], n.metadata["revision"], n.metadata["chunk_index"]): n for n in nodes}
    modes = [("dense", False)] if calibrate else [("dense", False), ("hybrid", False), ("hybrid", True)]
    rows = []
    async with open_store(settings) as (store, client):
        retriever = KnowledgeRetriever(store, client, settings)
        manifest = await read_manifest(client, settings) if calibrate else None
        # 各模式共享同一批真实查询向量；避免比较时重复计费。
        embeddings = await retriever.embedding.aget_text_embedding_batch([c["query"] for c in cases])
        semaphore = asyncio.Semaphore(concurrency)

        async def run_case(case, vector):
            async with semaphore:
                return await evaluate_case(case, vector)

        async def evaluate_case(case, vector):
            case_rows = []
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
                case_rows.append(dict(id=case["id"], group=case["group"], negative_kind=case["negative_kind"],
                    mode=mode + ("+rerank" if rerank else ""),
                    expected=expected, status=result.status, reason=result.reason, warnings=result.warnings,
                    hit_at_1=bool(matches and matches[0]), hit_at_4=any(matches),
                    score=result.sources[0].score if result.sources else 0,
                    valid_citations=valid, citations=len(result.sources), cross_model=cross,
                    elapsed_ms=round((time.perf_counter()-start)*1000),
                    sources=[dict(document_id=s.document_id, chunk_index=s.chunk_index) for s in result.sources]))
                if calibrate:
                    sparse = await retriever.recall(QueryBundle(query_str=case["query"], embedding=vector),
                        scope_filter(manifest, case["model_code"], case["category"]), "sparse")
                    case_rows[-1]["bm25_score"] = float(sparse[0].score or 0) if sparse else 0.0
            return case_rows

        batches = await asyncio.gather(*(run_case(case, vector) for case, vector in zip(cases, embeddings, strict=True)))
        rows = [row for batch in batches for row in batch]
    report = dict(embedding_model=settings.embed_model_name, dimension=settings.embed_dimension,
                  rerank_model=settings.rerank_model, generation=generation,
                  collection=settings.qdrant_collection, sparse_version=SPARSE_VERSION,
                  bm25_k1=BM25_K1, bm25_b=BM25_B,
                  min_dense_score=settings.rag_min_dense_score, min_bm25_score=settings.rag_min_bm25_score,
                  split=split, dataset_cases=len(cases), dataset_sha256=fingerprint(RETRIEVAL_PATH), concurrency=concurrency)
    if calibration_path:
        report["threshold_source_sha256"] = fingerprint(calibration_path)
    if calibrate:
        if any(r["status"] == "unavailable" for r in rows):
            raise RuntimeError("calibration dependency unavailable")
        if any(r["expected"] and r["status"] != "ok" for r in rows):
            raise RuntimeError("calibration positive cases have no candidates; check index before calibrating")
        threshold, correct = select_threshold(rows, "score", upper=1.0)
        bm25_threshold, bm25_correct = select_threshold(rows, "bm25_score")
        report.update(suggested_min_dense_score=threshold, suggested_min_bm25_score=bm25_threshold,
                      calibration_correct=correct, bm25_calibration_correct=bm25_correct,
                      hybrid_calibration_correct=sum(
                          ((r["score"] >= threshold) or (r["bm25_score"] >= bm25_threshold))
                          == bool(r["expected"]) for r in rows), cases=len(rows), rows=rows)
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
            false_accepts=sum(r["status"] == "ok" for r in negatives),
            false_accept_rate=sum(r["status"] == "ok" for r in negatives)/len(negatives) if negatives else None,
            false_rejects=sum(r["status"] == "insufficient_evidence" for r in positives),
            false_reject_rate=sum(r["status"] == "insufficient_evidence" for r in positives)/len(positives),
            citation_validity=sum(r["valid_citations"] for r in subset)/citations if citations else 0,
            cross_model=sum(r["cross_model"] for r in subset),
            errors=sum(r["status"] == "unavailable" for r in subset),
            rerank_fallbacks=sum("rerank_unavailable" in r["warnings"] for r in subset),
            bm25_recoveries=sum("bm25_recall_fallback" in r["warnings"] for r in subset),
            mean_retrieval_ms=round(sum(r["elapsed_ms"] for r in subset)/len(subset)),
        )
    report.update(summary=summaries, rows=rows,
        note="自建合成样本；相近改写同组。无答案放行统计检索门槛，不代表生成模型已编造答案；耗时不含共享查询向量批次。")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly call configured Qdrant and Bailian")
    parser.add_argument("--calibrate", action="store_true", help="use separate calibration cases, no env mutation")
    parser.add_argument("--thresholds-from", type=Path, help="仅本次使用匹配的校准报告阈值，不修改配置")
    parser.add_argument("--concurrency", type=int, default=1, help="同时评测的问题数，1—8，默认顺序执行")
    parser.add_argument("--out", type=Path, default=BACKEND_ROOT / "var" / "retrieval-evaluation.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; real embedding/rerank requests consume quota")
    if args.calibrate and args.thresholds_from:
        parser.error("--calibrate 与 --thresholds-from 分别运行")
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency 应在 1 到 8 之间")
    report = asyncio.run(evaluate(calibrate=args.calibrate, calibration_path=args.thresholds_from,
                                  concurrency=args.concurrency))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))
    if not args.calibrate and any(s["errors"] or s["cross_model"] or s["citation_validity"] < 1
                                  for s in report["summary"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
