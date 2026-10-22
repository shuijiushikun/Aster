"""显式运行索引；只写项目专属集合和 knowledge_documents，不重建业务表。"""

import asyncio
import json
from datetime import datetime, timezone

from llama_index.core.schema import MetadataMode
from qdrant_client import models
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db
from app.db.models import KnowledgeDocument
from app.rag.corpus import OWNER, load_corpus
from app.rag.embedding import make_embedding
from app.rag.store import (
    DENSE_VECTOR,
    MANIFEST_ID,
    SPARSE_VECTOR,
    condition,
    identity,
    open_store,
    read_manifest,
)


async def index_knowledge(settings=None) -> dict:
    settings = settings or get_settings()
    documents, nodes, generation = load_corpus()
    embedding = make_embedding(settings)
    async with open_store(settings) as (store, client):
        exists = await client.collection_exists(settings.qdrant_collection)
        if exists:
            manifest = await read_manifest(client, settings)
            info = await client.get_collection(settings.qdrant_collection)
            vectors = info.config.params.vectors
            if (not isinstance(vectors, dict) or DENSE_VECTOR not in vectors
                    or vectors[DENSE_VECTOR].size != settings.embed_dimension):
                raise ValueError("collection vector dimensions differ; use a new collection")
            active_filter = models.Filter(must=[condition("owner", OWNER), condition("record_type", "chunk"),
                                                condition("index_generation", generation)])
            count = (await client.count(settings.qdrant_collection, count_filter=active_filter, exact=True)).count
            if manifest.get("state") == "ready" and manifest.get("generation") == generation and count == len(nodes):
                return dict(status="unchanged", documents=len(documents), chunks=count, generation=generation)

        # 先完成全部向量化与维数校验，失败时不发布新目录。
        vectors = await embedding.aget_text_embedding_batch([n.get_content(MetadataMode.NONE) for n in nodes])
        for node, vector in zip(nodes, vectors, strict=True):
            node.embedding = vector
        if not exists:
            await client.create_collection(
                settings.qdrant_collection,
                vectors_config={DENSE_VECTOR: models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE)},
                sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams()},
            )
            await client.upsert(settings.qdrant_collection, points=[models.PointStruct(
                id=MANIFEST_ID, vector={}, payload={**identity(settings), "state": "building"})], wait=True)
            for field in ("owner", "record_type", "index_generation", "effective_status", "category",
                          "model_code", "document_id", "revision"):
                await client.create_payload_index(settings.qdrant_collection, field,
                                                  models.PayloadSchemaType.KEYWORD, wait=True)
        await store.async_add(nodes)
        written = await client.count(settings.qdrant_collection, exact=True, count_filter=models.Filter(must=[
            condition("owner", OWNER), condition("record_type", "chunk"), condition("index_generation", generation),
        ]))
        if written.count != len(nodes):
            raise RuntimeError(f"index write incomplete: expected {len(nodes)}, found {written.count}")

        # 仅同步该本地语料的目录；不修改订单/工单/客户。
        async with db.SessionLocal() as session, session.begin():
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
        manifest = {**identity(settings), "state": "ready", "generation": generation,
                    "documents": [d.model_dump() for d in documents], "chunks": len(nodes)}
        await client.upsert(settings.qdrant_collection, points=[models.PointStruct(
            id=MANIFEST_ID, vector={}, payload=manifest)], wait=True)
        # 只回收本项目已被新目录替换的片段，保留其他集合及非项目记录。
        await client.delete(settings.qdrant_collection, points_selector=models.FilterSelector(filter=models.Filter(
            must=[condition("owner", OWNER), condition("record_type", "chunk")],
            must_not=[condition("index_generation", generation)],
        )), wait=True)
        return dict(status="indexed", documents=len(documents), chunks=len(nodes), generation=generation,
                    embedding_model=settings.embed_model_name, embedding_dimension=len(vectors[0]))


async def main():
    try:
        print(json.dumps(await index_knowledge(), ensure_ascii=False))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
