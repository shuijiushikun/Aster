"""重建、发布失败与并发发布时，已选定的索引快照保持可读。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from llama_index.core.embeddings import MockEmbedding
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import AsyncQdrantClient, models

from app.config import Settings
from app.db import session as db
from app.db.models import KnowledgeDocument
from app.rag import index_knowledge as indexing
from app.rag import retriever as retrieval
from app.rag.corpus import load_corpus
from app.rag.sparse import BM25Encoder, average_document_length, reciprocal_rank_fusion
from app.rag.store import DENSE_VECTOR, MANIFEST_ID, SPARSE_VECTOR, identity, read_manifest


def write_document(directory, text):
    meta = dict(document_id="GEN-TEST", title="耳机资料", revision="1", category="digital",
                model_code="HP-01", effective_status="active", references=["https://example.test"])
    (directory / "headphones.md").write_text(
        "---\n" + json.dumps(meta) + "\n---\n## 续航\n" + text, encoding="utf-8")


@pytest.fixture
async def snapshots(engine, tmp_path, monkeypatch):
    directory = tmp_path / "knowledge"
    directory.mkdir()
    write_document(directory, "电池续航为6小时。")
    settings = Settings(_env_file=None, embed_model_name="test", embed_dimension=2,
        embed_api_key="unused-test-value", qdrant_url="http://unused.test", qdrant_collection="generations",
        rag_min_dense_score=0, rag_rerank_enabled=False, rag_timeout_s=10)
    embedding = MockEmbedding(embed_dim=2)
    client = AsyncQdrantClient(location=":memory:")

    def store(average_length=None):
        encoder = BM25Encoder(average_length)
        return QdrantVectorStore(collection_name=settings.qdrant_collection, aclient=client,
            enable_hybrid=True, sparse_doc_fn=encoder.documents, sparse_query_fn=encoder.queries,
            hybrid_fusion_fn=reciprocal_rank_fusion, dense_vector_name=DENSE_VECTOR,
            sparse_vector_name=SPARSE_VECTOR, index_doc_id=False, max_retries=1)

    @asynccontextmanager
    async def open_store(settings, *, bm25_average_length=None):
        yield store(bm25_average_length), client

    monkeypatch.setattr(indexing, "open_store", open_store)
    monkeypatch.setattr(indexing, "load_corpus", lambda: load_corpus(directory))
    monkeypatch.setattr(indexing, "make_embedding", lambda settings: embedding)
    monkeypatch.setattr(retrieval, "make_embedding", lambda settings: embedding)
    try:
        yield SimpleNamespace(settings=settings, client=client, directory=directory, store=store)
    finally:
        await client.close()


def test_edited_content_without_revision_change_has_disjoint_point_ids(tmp_path):
    write_document(tmp_path, "电池续航为6小时。")
    _, first, generation = load_corpus(tmp_path)
    write_document(tmp_path, "电池续航为8小时。")
    _, second, next_generation = load_corpus(tmp_path)
    assert generation != next_generation
    assert {n.node_id for n in first}.isdisjoint(n.node_id for n in second)


async def test_old_snapshot_survives_rebuild_and_duplicate_run_is_unchanged(snapshots, monkeypatch):
    s = snapshots
    await indexing.index_knowledge(s.settings)
    old = await read_manifest(s.client, s.settings)
    write_document(s.directory, "电池续航为8小时。")
    await indexing.index_knowledge(s.settings)
    new = await read_manifest(s.client, s.settings)
    assert old["collection"] != new["collection"]
    assert await s.client.collection_exists(old["collection"])
    assert (await indexing.index_knowledge(s.settings))["status"] == "unchanged"

    async def pinned(*args):
        return old
    monkeypatch.setattr(retrieval, "read_manifest", pinned)
    worker = retrieval.KnowledgeRetriever(s.store(), s.client, s.settings)
    result = await worker.retrieve("续航", model_code="HP-01", category="digital", query_embedding=[0, 1])
    assert result.status == "ok" and "6小时" in result.sources[0].excerpt


async def test_hybrid_query_keeps_one_snapshot_during_publication(snapshots, monkeypatch):
    s = snapshots
    await indexing.index_knowledge(s.settings)
    worker = retrieval.KnowledgeRetriever(s.store(), s.client, s.settings)
    original = worker.recall
    selected = []

    async def recall(bundle, filters, mode, **kwargs):
        selected.append(kwargs.get("index"))
        rows = await original(bundle, filters, mode, **kwargs)
        if mode == "default":
            write_document(s.directory, "电池续航为8小时。")
            await indexing.index_knowledge(s.settings)
        return rows

    monkeypatch.setattr(worker, "recall", recall)
    result = await worker.retrieve("续航", model_code="HP-01", category="digital",
                                   mode="hybrid", query_embedding=[0, 1])
    assert result.status == "ok" and all("6小时" in source.excerpt for source in result.sources)
    assert len(selected) == 2 and selected[0] is not None and selected[0] is selected[1]


async def test_partial_build_does_not_change_old_bm25_scores_or_catalog(snapshots, monkeypatch):
    s = snapshots
    await indexing.index_knowledge(s.settings)
    old = await read_manifest(s.client, s.settings)
    indices, values = BM25Encoder().queries(["续航"])
    query = models.SparseVector(indices=indices[0], values=values[0])
    before = await s.client.query_points(old.get("collection", s.settings.qdrant_collection),
                                        query=query, using=SPARSE_VECTOR)
    assert before.points
    write_document(s.directory, "电池续航为8小时。" + "\n## 使用\n连接设备后播放音乐。" * 40)
    original = QdrantVectorStore.async_add

    async def partial(store, nodes, **kwargs):
        await original(store, nodes[:1], **kwargs)
        raise RuntimeError("interrupted build")

    monkeypatch.setattr(QdrantVectorStore, "async_add", partial)
    with pytest.raises(RuntimeError, match="interrupted"):
        await indexing.index_knowledge(s.settings)
    assert await read_manifest(s.client, s.settings) == old
    async with db.SessionLocal() as session:
        assert (await session.get(KnowledgeDocument, "GEN-TEST")).content_hash == old["documents"][0]["content_hash"]
    after = await s.client.query_points(old.get("collection", s.settings.qdrant_collection),
                                       query=query, using=SPARSE_VECTOR)
    assert after == before


@pytest.mark.parametrize("applied", [False, True])
async def test_failed_publication_restores_manifest_and_rolls_back_catalog(snapshots, monkeypatch, applied):
    s = snapshots
    await indexing.index_knowledge(s.settings)
    old = await read_manifest(s.client, s.settings)
    write_document(s.directory, "电池续航为8小时。")
    original = s.client.upsert
    failed = False

    async def publish(collection_name, points, **kwargs):
        nonlocal failed
        if (not failed and collection_name == s.settings.qdrant_collection
                and points[0].payload.get("state") == "ready"):
            failed = True
            if applied:
                await original(collection_name, points=points, **kwargs)
            raise RuntimeError("publication response lost")
        return await original(collection_name, points=points, **kwargs)

    monkeypatch.setattr(s.client, "upsert", publish)
    with pytest.raises(RuntimeError, match="publication response lost"):
        await indexing.index_knowledge(s.settings)
    assert await read_manifest(s.client, s.settings) == old
    async with db.SessionLocal() as session:
        assert (await session.get(KnowledgeDocument, "GEN-TEST")).content_hash == old["documents"][0]["content_hash"]


async def test_only_one_indexer_can_build_and_lock_is_released(snapshots, monkeypatch):
    s = snapshots
    entered, resume = asyncio.Event(), asyncio.Event()

    async def embeddings(texts):
        entered.set()
        await resume.wait()
        return [[0.5, 0.5] for _ in texts]

    monkeypatch.setattr(indexing, "make_embedding", lambda settings:
                        SimpleNamespace(aget_text_embedding_batch=embeddings))
    first = asyncio.create_task(indexing.index_knowledge(s.settings))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        with pytest.raises(RuntimeError, match="index.*running"):
            await indexing.index_knowledge(s.settings)
    finally:
        resume.set()
        await first
    assert (await indexing.index_knowledge(s.settings))["status"] == "unchanged"


async def test_legacy_layout_is_upgraded_without_deleting_its_chunks(snapshots):
    s = snapshots
    docs, nodes, generation = load_corpus(s.directory)
    for node in nodes:
        node.embedding = [0.5, 0.5]
    await s.client.create_collection(s.settings.qdrant_collection,
        vectors_config={DENSE_VECTOR: models.VectorParams(size=2, distance=models.Distance.COSINE)},
        sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)})
    average_length = average_document_length([n.text for n in nodes])
    await s.store(average_length).async_add(nodes)
    legacy = {**identity(s.settings), "state": "ready", "generation": generation,
              "documents": [d.model_dump() for d in docs], "chunks": len(nodes),
              "bm25_average_length": average_length}
    await s.client.upsert(s.settings.qdrant_collection,
        points=[models.PointStruct(id=MANIFEST_ID, vector={}, payload=legacy)])
    assert await read_manifest(s.client, s.settings) == legacy
    assert (await indexing.index_knowledge(s.settings))["status"] == "indexed"
    current = await read_manifest(s.client, s.settings)
    assert current["collection"] != s.settings.qdrant_collection
    assert len(await s.client.retrieve(s.settings.qdrant_collection, [n.node_id for n in nodes])) == len(nodes)


async def test_cancelling_a_build_preserves_old_snapshot_and_releases_lock(snapshots, monkeypatch):
    s = snapshots
    await indexing.index_knowledge(s.settings)
    old = await read_manifest(s.client, s.settings)
    write_document(s.directory, "电池续航为8小时。")
    entered = asyncio.Event()
    original = indexing.make_embedding

    async def embeddings(texts):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(indexing, "make_embedding", lambda settings:
                        SimpleNamespace(aget_text_embedding_batch=embeddings))
    task = asyncio.create_task(indexing.index_knowledge(s.settings))
    await asyncio.wait_for(entered.wait(), 15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await read_manifest(s.client, s.settings) == old
    monkeypatch.setattr(indexing, "make_embedding", original)
    assert (await indexing.index_knowledge(s.settings))["status"] == "indexed"


@pytest.mark.parametrize("change", ["foreign_collection", "building", "generation"])
async def test_invalid_snapshot_is_rejected_before_retrieval(snapshots, change):
    s = snapshots
    await indexing.index_knowledge(s.settings)
    manifest = await read_manifest(s.client, s.settings)
    if change == "foreign_collection":
        collection, payload = s.settings.qdrant_collection, {"collection": "another_project"}
    else:
        collection = manifest["collection"]
        payload = {"state": "building"} if change == "building" else {"generation": "other"}
    await s.client.set_payload(collection, payload, points=[MANIFEST_ID])
    with pytest.raises(ValueError):
        await read_manifest(s.client, s.settings)
    worker = retrieval.KnowledgeRetriever(s.store(), s.client, s.settings)
    result = await worker.retrieve("续航", model_code="HP-01", category="digital", query_embedding=[0, 1])
    assert result.status == "unavailable" and not result.sources


async def test_catalog_commit_failure_restores_the_previous_publication(snapshots):
    from sqlalchemy import event

    from app.db.turns import TurnSession

    s = snapshots
    await indexing.index_knowledge(s.settings)
    old = await read_manifest(s.client, s.settings)
    write_document(s.directory, "电池续航为8小时。")

    def fail_catalog(session):
        if any(isinstance(row, KnowledgeDocument) for row in session.dirty):
            raise RuntimeError("catalog commit failed")

    event.listen(TurnSession, "before_commit", fail_catalog)
    try:
        with pytest.raises(RuntimeError, match="catalog commit failed"):
            await indexing.index_knowledge(s.settings)
    finally:
        event.remove(TurnSession, "before_commit", fail_catalog)
    assert await read_manifest(s.client, s.settings) == old
    async with db.SessionLocal() as session:
        assert (await session.get(KnowledgeDocument, "GEN-TEST")).content_hash == old["documents"][0]["content_hash"]


async def test_unchanged_generation_repairs_the_database_catalog(snapshots):
    s = snapshots
    await indexing.index_knowledge(s.settings)
    manifest = await read_manifest(s.client, s.settings)
    async with db.SessionLocal() as session, session.begin():
        row = await session.get(KnowledgeDocument, "GEN-TEST")
        row.content_hash, row.status = "stale", "inactive"
    assert (await indexing.index_knowledge(s.settings))["status"] == "unchanged"
    async with db.SessionLocal() as session:
        row = await session.get(KnowledgeDocument, "GEN-TEST")
        assert row.status == "active" and row.content_hash == manifest["documents"][0]["content_hash"]
