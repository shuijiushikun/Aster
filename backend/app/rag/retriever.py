"""只返回可追溯证据。型号/品类由调用方从可信订单或商品目录取得。"""

import asyncio
import logging

import httpx
from llama_index.core import VectorStoreIndex
from llama_index.core.schema import MetadataMode, QueryBundle
from qdrant_client import models

from app.config import get_settings
from app.rag.corpus import GENERAL_MODEL, OWNER
from app.rag.embedding import make_embedding
from app.rag.schemas import Evidence, RetrievalResult
from app.rag.store import condition, read_manifest

logger = logging.getLogger(__name__)


def scope_filter(manifest: dict, model_code: str | None, category: str | None) -> models.Filter:
    scopes = [models.Filter(must=[condition("category", "general"), condition("model_code", GENERAL_MODEL)])]
    if model_code and category:
        scopes.append(models.Filter(must=[condition("category", category), condition("model_code", model_code)]))
    revisions = [models.Filter(must=[condition("document_id", d["document_id"]), condition("revision", d["revision"])])
                 for d in manifest["documents"]]
    return models.Filter(must=[condition("owner", OWNER), condition("record_type", "chunk"),
                               condition("effective_status", "active"),
                               condition("index_generation", manifest["generation"]),
                               models.Filter(should=scopes), models.Filter(should=revisions)])


class KnowledgeRetriever:
    def __init__(self, store, client, settings=None):
        self.settings = settings or get_settings()
        self.client = client
        self.embedding = make_embedding(self.settings)
        self.index = VectorStoreIndex.from_vector_store(store, embed_model=self.embedding)

    async def _rank(self, query, candidates):
        s = self.settings
        async with httpx.AsyncClient(timeout=s.rag_timeout_s) as client:
            response = await client.post(s.rerank_url, headers={
                "Authorization": "Bearer " + (s.rerank_api_key or s.embed_api_key),
            }, json={"model": s.rerank_model, "input": {"query": query, "documents": [
                n.node.get_content(MetadataMode.NONE) for n in candidates
            ]}, "parameters": {"top_n": len(candidates), "return_documents": False}})
        response.raise_for_status()
        rows = response.json()["output"]["results"]
        indices = [r["index"] for r in rows]
        if sorted(indices) != list(range(len(candidates))):
            raise ValueError("invalid rerank indices")
        return [(candidates[r["index"]], float(r["relevance_score"])) for r in rows]

    async def retrieve(self, query: str, *, model_code: str | None = None, category: str | None = None,
                       mode: str | None = None, rerank: bool | None = None,
                       query_embedding: list[float] | None = None) -> RetrievalResult:
        """无可信型号时只检索通用文档；未知或品类不符的型号直接要求核实。"""
        s = self.settings
        mode = mode or s.rag_search_mode
        rerank = s.rag_rerank_enabled if rerank is None else rerank
        if mode not in ("dense", "hybrid"):
            raise ValueError("RAG_SEARCH_MODE must be dense or hybrid")
        base = dict(query=query, model_code=model_code)
        if not query.strip() or len(query) > 2000:
            return RetrievalResult(**base, status="insufficient_evidence", reason="invalid_query")
        try:
            async with asyncio.timeout(s.rag_timeout_s):
                manifest = await read_manifest(self.client, s)
                if manifest.get("state") != "ready":
                    return RetrievalResult(**base, status="unavailable", reason="index_not_ready")
                if model_code and not any(d["model_code"] == model_code and d["category"] == category
                                          for d in manifest["documents"]):
                    return RetrievalResult(**base, status="insufficient_evidence", reason="unverified_model_scope")
                filters = scope_filter(manifest, model_code, category)
                bundle = QueryBundle(query_str=query, embedding=query_embedding)
                # 同一 query bundle 复用 Embedding；拒答只用校准过的 dense 分数，不比较 RRF/Rerank 分数。
                dense = self.index.as_retriever(
                    similarity_top_k=s.rag_candidate_k, vector_store_query_mode="default",
                    vector_store_kwargs={"qdrant_filters": filters},
                )
                candidates = await dense.aretrieve(bundle)
                if not candidates or (candidates[0].score or 0) < s.rag_min_dense_score:
                    return RetrievalResult(**base, status="insufficient_evidence", reason="no_relevant_evidence")
                score_kind, warnings = "cosine", []
                if mode == "hybrid":
                    candidates = await self.index.as_retriever(
                        similarity_top_k=s.rag_candidate_k, sparse_top_k=s.rag_candidate_k,
                        hybrid_top_k=s.rag_candidate_k, vector_store_query_mode="hybrid",
                        vector_store_kwargs={"qdrant_filters": filters},
                    ).aretrieve(bundle)
                    score_kind = "rrf"
                ranked = [(n, float(n.score or 0)) for n in candidates]
                if rerank and candidates:
                    try:
                        ranked = await self._rank(query, candidates)
                        score_kind = "rerank"
                    except (httpx.HTTPError, KeyError, TypeError, ValueError):
                        warnings.append("rerank_unavailable")
                sources = []
                active = {d["document_id"]: d for d in manifest["documents"]}
                for node, score in ranked[:s.rag_top_k]:
                    m = node.node.metadata
                    doc = active.get(m.get("document_id"))
                    if (doc is None or doc["revision"] != m.get("revision")
                            or doc["content_hash"] != m.get("content_hash")
                            or m.get("index_generation") != manifest["generation"]
                            or (doc["model_code"] is not None and
                                (doc["model_code"] != model_code or doc["category"] != category))):
                        raise ValueError("evidence metadata mismatch")
                    sources.append(Evidence(document_id=doc["document_id"], title=doc["title"],
                        revision=doc["revision"], chunk_index=m["chunk_index"], category=doc["category"],
                        model_code=doc["model_code"], score=score, score_kind=score_kind,
                        excerpt=node.node.get_content(MetadataMode.NONE)[:1800], source_path=doc["source_path"],
                        references=doc["references"]))
                return RetrievalResult(**base, status="ok" if sources else "insufficient_evidence",
                                       sources=sources, warnings=warnings)
        except Exception as exc:
            # 不把供应商原始响应、请求头或数据库 URL 返回给模型/日志。
            logger.warning("knowledge retrieval unavailable: %s", type(exc).__name__)
            return RetrievalResult(**base, status="unavailable", reason="retrieval_dependency_error")
