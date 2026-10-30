"""显式运行索引；只写项目专属集合和 knowledge_documents，不重建业务表。"""

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from llama_index.core.schema import MetadataMode
from qdrant_client import models
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db
from app.db.models import KnowledgeDocument
from app.rag.corpus import OWNER, load_corpus
from app.rag.embedding import make_embedding
from app.rag.index_lock import index_lock
from app.rag.sparse import average_document_length
from app.rag.store import (
    DENSE_VECTOR,
    MANIFEST_ID,
    SPARSE_VECTOR,
    condition,
    identity,
    open_store,
    read_manifest,
    vector_store,
)


async def index_knowledge(settings=None) -> dict:
    settings = settings or get_settings()
    async with index_lock(settings) as verify:
        return await _index_knowledge(settings, verify)


async def _index_knowledge(settings, verify) -> dict:
    documents, nodes, generation = load_corpus()
    texts = [n.get_content(MetadataMode.NONE) for n in nodes]
    average_length = average_document_length(texts)
    embedding = make_embedding(settings)
    async with open_store(settings) as (_, client):
        exists = await client.collection_exists(settings.qdrant_collection)
        previous = {**identity(settings), "state": "building"}
        if exists:
            manifest = await read_manifest(client, settings)
            previous = manifest
            active_filter = models.Filter(must=[condition("owner", OWNER), condition("record_type", "chunk"),
                                                condition("index_generation", generation)])
            target = manifest.get("collection")
            count = (await client.count(target or settings.qdrant_collection,
                                        count_filter=active_filter, exact=True)).count
            if target and manifest.get("generation") == generation and count == len(nodes):
                await sync_catalog(documents)
                return dict(status="unchanged", documents=len(documents), chunks=count, generation=generation)

        # 先完成全部向量化与维数校验，失败时不发布新目录。
        vectors = await embedding.aget_text_embedding_batch(texts)
        for node, vector in zip(nodes, vectors, strict=True):
            if len(vector) != settings.embed_dimension:
                raise ValueError("embedding dimension mismatch")
            node.embedding = vector
        if not exists:
            await client.create_collection(settings.qdrant_collection, vectors_config={})
            await client.upsert(settings.qdrant_collection, points=[models.PointStruct(
                id=MANIFEST_ID, vector={}, payload=previous)], wait=True)
        target = f"{settings.qdrant_collection}__g_{generation[:16]}_{uuid4().hex}"
        await client.create_collection(target,
            vectors_config={DENSE_VECTOR: models.VectorParams(size=settings.embed_dimension,
                                                            distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)})
        await client.upsert(target, points=[models.PointStruct(id=MANIFEST_ID, vector={},
            payload={**identity(settings), "state": "building", "generation": generation})], wait=True)
        for field in ("owner", "record_type", "index_generation", "effective_status", "category",
                      "model_code", "document_id", "revision"):
            await client.create_payload_index(target, field, models.PayloadSchemaType.KEYWORD, wait=True)
        store = vector_store(settings, client, collection_name=target, bm25_average_length=average_length)
        await store.async_add(nodes)
        written = await client.count(target, exact=True, count_filter=models.Filter(must=[
            condition("owner", OWNER), condition("record_type", "chunk"), condition("index_generation", generation),
        ]))
        if written.count != len(nodes):
            raise RuntimeError(f"index write incomplete: expected {len(nodes)}, found {written.count}")

        manifest = {**identity(settings), "state": "ready", "generation": generation, "collection": target,
                    "bm25_average_length": average_length,
                    "documents": [d.model_dump() for d in documents], "chunks": len(nodes)}
        await client.upsert(target, points=[models.PointStruct(id=MANIFEST_ID, vector={}, payload=manifest)], wait=True)
        await verify()
        try:
            async with db.SessionLocal() as session, session.begin():
                await sync_catalog(documents, session)
                await client.upsert(settings.qdrant_collection, points=[models.PointStruct(
                    id=MANIFEST_ID, vector={}, payload=manifest)], wait=True)
        except BaseException:
            # 请求可能已送达 Qdrant；同时覆盖“未写入”和“已写入但响应丢失”。
            await verify()
            await client.upsert(settings.qdrant_collection, points=[models.PointStruct(
                id=MANIFEST_ID, vector={}, payload=previous)], wait=True)
            raise
        # 旧快照和未完成的构建保留，避免删掉在途查询正在使用的 collection。
        return dict(status="indexed", documents=len(documents), chunks=len(nodes), generation=generation,
                    collection=target, embedding_model=settings.embed_model_name,
                    embedding_dimension=settings.embed_dimension)


async def sync_catalog(documents, session=None):
    if session is None:
        async with db.SessionLocal() as session, session.begin():
            await sync_catalog(documents, session)
        return
    # 仅同步该本地语料的目录；不修改订单/工单/客户。
    old = (await session.scalars(select(KnowledgeDocument))).all()
    active_ids = {d.document_id for d in documents}
    for row in old:
        if row.source_path.startswith("data/knowledge/") and row.id not in active_ids:
            row.status = "inactive"
    for doc in documents:
        row = await session.get(KnowledgeDocument, doc.document_id)
        fields = dict(title=doc.title, category=doc.category, model_code=doc.model_code,
                      revision=doc.revision, status="active", source_path=doc.source_path,
                      content_hash=doc.content_hash, indexed_at=datetime.now(timezone.utc))
        if row is None:
            session.add(KnowledgeDocument(id=doc.document_id, **fields))
        else:
            for key, value in fields.items():
                setattr(row, key, value)


async def main():
    try:
        print(json.dumps(await index_knowledge(), ensure_ascii=False))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
