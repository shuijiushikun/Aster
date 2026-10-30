from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid5

from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import AsyncQdrantClient, models

from app.rag.corpus import INDEX_VERSION, OWNER
from app.rag.sparse import BM25_B, BM25_K1, SPARSE_VERSION, BM25Encoder, reciprocal_rank_fusion

MANIFEST_ID = str(uuid5(NAMESPACE_URL, f"{OWNER}/manifest"))
DENSE_VECTOR = "text-dense"
SPARSE_VECTOR = "text-sparse"


def identity(settings) -> dict:
    return dict(owner=OWNER, index_version=INDEX_VERSION, embedding_model=settings.embed_model_name,
                embedding_dimension=settings.embed_dimension,
                embedding_endpoint=settings.embed_base_url.rstrip("/"), sparse_version=SPARSE_VERSION,
                bm25_k1=BM25_K1, bm25_b=BM25_B)


async def read_manifest(client, settings) -> dict:
    rows = await client.retrieve(settings.qdrant_collection, [MANIFEST_ID])
    if not rows or not rows[0].payload:
        raise ValueError("collection has no project manifest; choose a new QDRANT_COLLECTION")
    manifest = rows[0].payload
    if any(manifest.get(k) != v for k, v in identity(settings).items()):
        raise ValueError("index model/dimension/version mismatch; choose a new QDRANT_COLLECTION")
    target = manifest.get("collection")
    if target:
        prefix = f"{settings.qdrant_collection}__g_{manifest['generation'][:16]}_"
        if not target.startswith(prefix):
            raise ValueError("snapshot collection outside project namespace")
        snapshot = await client.retrieve(target, [MANIFEST_ID])
        if not snapshot or snapshot[0].payload != manifest or manifest.get("state") != "ready":
            raise ValueError("snapshot manifest mismatch")
    return manifest


def condition(key: str, value: str) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def vector_store(settings, client, *, collection_name=None, bm25_average_length=None):
    encoder = BM25Encoder(bm25_average_length)
    return QdrantVectorStore(
        collection_name=collection_name or settings.qdrant_collection, aclient=client,
        enable_hybrid=True, sparse_doc_fn=encoder.documents, sparse_query_fn=encoder.queries,
        hybrid_fusion_fn=reciprocal_rank_fusion, index_doc_id=False, batch_size=10,
        max_retries=1, dense_vector_name=DENSE_VECTOR, sparse_vector_name=SPARSE_VECTOR,
    )


@asynccontextmanager
async def open_store(settings, *, bm25_average_length: float | None = None):
    if not settings.qdrant_url:
        raise ValueError("QDRANT_URL is required")
    kwargs = dict(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None,
                  timeout=int(settings.rag_timeout_s))
    # Async SDK 的构造器默认也会做同步版本探测；这里禁用，连接由实际异步请求核实。
    aclient = AsyncQdrantClient(**kwargs, check_compatibility=False)
    try:
        store = vector_store(settings, aclient, bm25_average_length=bm25_average_length)
        yield store, aclient
    finally:
        await aclient.close()
