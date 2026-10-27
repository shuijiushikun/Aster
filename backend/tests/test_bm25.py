"""BM25 权重、Qdrant IDF 与混合召回准入；外部服务不参与这些用例。"""

from types import SimpleNamespace

import httpx
import pytest
from llama_index.core.schema import NodeWithScore
from qdrant_client import AsyncQdrantClient, models

from app.rag.corpus import load_corpus
from app.rag.sparse import BM25Encoder, token_counts


def test_bm25_saturation_length_normalization_and_batch_independence():
    encoder = BM25Encoder(average_length=2)
    indices, values = encoder.documents(["pair pair", "pair filler filler filler"])
    token = next(iter(token_counts("pair")))
    repeated = dict(zip(indices[0], values[0], strict=True))[token]
    long_document = dict(zip(indices[1], values[1], strict=True))[token]
    assert repeated == pytest.approx(2 * 2.2 / (2 + 1.2))
    assert long_document == pytest.approx(2.2 / (1 + 1.2 * (0.25 + 0.75 * 2)))
    assert repeated > long_document
    assert encoder.documents(["pair pair"]) == ([indices[0]], [values[0]])


def test_query_encoding_preserves_models_and_deduplicates_terms():
    encoder = BM25Encoder()
    assert encoder.queries(["HP-01 蓝牙配对"]) == encoder.queries(["hp-01 hp-01 蓝牙配对"])
    assert set(token_counts("蓝牙配对")) == set(token_counts("蓝牙 牙配 配对"))
    assert encoder.queries([""]) == ([[]], [[]])
    with pytest.raises(ValueError, match="average_length"):
        encoder.documents(["pair"])


async def test_qdrant_applies_idf_to_rare_terms():
    texts = ["common rare", "common", "common"]
    encoder = BM25Encoder(average_length=sum(sum(token_counts(text).values()) for text in texts) / 3)
    indices, values = encoder.documents(texts)
    client = AsyncQdrantClient(location=":memory:")
    try:
        await client.create_collection("bm25", vectors_config={}, sparse_vectors_config={
            "terms": models.SparseVectorParams(modifier=models.Modifier.IDF)})
        await client.upsert("bm25", points=[models.PointStruct(id=i, vector={"terms":
            models.SparseVector(indices=ids, values=weights)})
            for i, (ids, weights) in enumerate(zip(indices, values, strict=True))])
        scores = {}
        for query in ("common", "rare"):
            ids, weights = encoder.queries([query])
            result = await client.query_points("bm25", using="terms",
                query=models.SparseVector(indices=ids[0], values=weights[0]), limit=3)
            scores[query] = next(point.score for point in result.points if point.id == 0)
        assert scores["rare"] > scores["common"]
    finally:
        await client.close()


@pytest.fixture
def worker(monkeypatch):
    from app.rag import retriever as module

    docs, nodes, generation = load_corpus()
    node = next(n for n in nodes if n.metadata["model_code"] == "HP-01")

    async def manifest(*args):
        return {"state": "ready", "generation": generation, "documents": [d.model_dump() for d in docs]}

    monkeypatch.setattr(module, "read_manifest", manifest)
    instance = module.KnowledgeRetriever.__new__(module.KnowledgeRetriever)
    instance.settings = SimpleNamespace(rag_search_mode="hybrid", rag_rerank_enabled=False,
        rag_timeout_s=2, rag_candidate_k=4, rag_top_k=4, rag_min_dense_score=0.8,
        rag_min_bm25_score=2.0)
    instance.client = None
    return instance, node


def scripted_scores(worker, dense_score, bm25_score):
    instance, node = worker

    def retriever(**kwargs):
        async def retrieve(bundle):
            score = bm25_score if kwargs["vector_store_query_mode"] == "sparse" else dense_score
            return [NodeWithScore(node=node, score=score)] if score else []
        return SimpleNamespace(aretrieve=retrieve)

    instance.index = SimpleNamespace(as_retriever=retriever)
    return instance


async def test_strong_bm25_can_recover_weak_dense_match(worker):
    instance = scripted_scores(worker, 0.2, 3.0)
    result = await instance.retrieve("左右声道平衡", model_code="HP-01", category="digital")
    assert result.status == "ok" and result.sources[0].model_code == "HP-01"
    assert result.sources[0].score_kind == "rrf"
    assert "bm25_recall_fallback" in result.warnings
    dense = await instance.retrieve("左右声道平衡", model_code="HP-01", category="digital", mode="dense")
    assert dense.status == "insufficient_evidence"


async def test_weak_branches_still_abstain_and_uncalibrated_bm25_cannot_bypass_gate(worker):
    instance = scripted_scores(worker, 0.2, 0.5)
    result = await instance.retrieve("芯片焊点电压", model_code="HP-01", category="digital")
    assert result.status == "insufficient_evidence" and not result.sources
    instance = scripted_scores(worker, 0.2, 3.0)
    instance.settings.rag_min_bm25_score = 0
    result = await instance.retrieve("芯片焊点电压", model_code="HP-01", category="digital")
    assert result.status == "insufficient_evidence"


async def test_bm25_fallback_survives_rerank_failure(worker, monkeypatch):
    instance = scripted_scores(worker, 0.2, 3.0)
    instance.settings.rag_rerank_enabled = True

    async def failed(*args):
        raise httpx.ConnectError("rerank offline")

    monkeypatch.setattr(instance, "_rank", failed)
    result = await instance.retrieve("左右声道平衡", model_code="HP-01", category="digital")
    assert result.status == "ok" and result.sources[0].score_kind == "rrf"
    assert {"bm25_recall_fallback", "rerank_unavailable"}.issubset(result.warnings)


def test_each_branch_has_its_own_calibrated_threshold():
    from app.rag.evaluate import select_threshold

    rows = [{"expected": "doc", "dense": 0.8, "bm25": 8.0},
            {"expected": "doc", "dense": 0.9, "bm25": 6.0},
            {"expected": None, "dense": 0.6, "bm25": 2.0}]
    dense, dense_correct = select_threshold(rows, "dense", upper=1.0)
    bm25, bm25_correct = select_threshold(rows, "bm25")
    assert 0.6 < dense <= 0.8 and 2.0 < bm25 <= 6.0
    assert dense_correct == bm25_correct == 3


async def test_indexing_and_hybrid_retrieval_with_real_local_qdrant(engine, monkeypatch):
    from contextlib import asynccontextmanager

    from llama_index.core.embeddings import MockEmbedding
    from llama_index.vector_stores.qdrant import QdrantVectorStore

    from app.config import Settings
    from app.rag import index_knowledge as indexing
    from app.rag import retriever as retrieval
    from app.rag.sparse import reciprocal_rank_fusion
    from app.rag.store import DENSE_VECTOR, MANIFEST_ID, SPARSE_VECTOR, read_manifest

    settings = Settings(_env_file=None, embed_model_name="test", embed_dimension=2,
        embed_api_key="unused-test-value", qdrant_url="http://unused.test", qdrant_collection="bm25",
        rag_min_dense_score=0.99, rag_min_bm25_score=0.05, rag_rerank_enabled=False)
    embedding = MockEmbedding(embed_dim=2)
    monkeypatch.setattr(indexing, "make_embedding", lambda settings: embedding)
    monkeypatch.setattr(retrieval, "make_embedding", lambda settings: embedding)
    client = AsyncQdrantClient(location=":memory:")

    def make_store(average_length=None):
        encoder = BM25Encoder(average_length)
        return QdrantVectorStore(collection_name="bm25", aclient=client, enable_hybrid=True,
            sparse_doc_fn=encoder.documents, sparse_query_fn=encoder.queries,
            hybrid_fusion_fn=reciprocal_rank_fusion, dense_vector_name=DENSE_VECTOR,
            sparse_vector_name=SPARSE_VECTOR, index_doc_id=False, max_retries=1)

    @asynccontextmanager
    async def open_store(settings, *, bm25_average_length=None):
        yield make_store(bm25_average_length), client

    monkeypatch.setattr(indexing, "open_store", open_store)
    try:
        first = await indexing.index_knowledge(settings)
        second = await indexing.index_knowledge(settings)
        assert first["status"] == "indexed" and second["status"] == "unchanged"
        manifest = await read_manifest(client, settings)
        assert manifest["bm25_average_length"] > 0
        config = (await client.get_collection("bm25")).config.params
        assert config.sparse_vectors[SPARSE_VECTOR].modifier == models.Modifier.IDF
        worker = retrieval.KnowledgeRetriever(make_store(), client, settings)
        result = await worker.retrieve("左右声道平衡", model_code="HP-01", category="digital",
                                      query_embedding=[0.0, 1.0])
        assert result.status == "ok" and "bm25_recall_fallback" in result.warnings
        assert any(s.document_id == "HP01-TROUBLESHOOT" for s in result.sources)
        assert all(s.model_code in (None, "HP-01") for s in result.sources)
        assert all(s.revision == next(d["revision"] for d in manifest["documents"]
                                     if d["document_id"] == s.document_id) for s in result.sources)
        await client.set_payload("bm25", payload={"sparse_version": "zh-bigram-latin-logtf-v1"},
                                 points=[MANIFEST_ID])
        with pytest.raises(ValueError, match="version mismatch"):
            await read_manifest(client, settings)
    finally:
        await client.close()
