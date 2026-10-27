"""只验证本地文档契约；真实向量库/百炼验证见 app.rag.evaluate。"""

import json

import pytest
from llama_index.core.vector_stores.utils import node_to_metadata_dict

from app.rag.corpus import load_corpus
from app.rag.schemas import RetrievalResult
from app.rag.sparse import BM25Encoder


def test_corpus_stable_ids_and_provenance():
    docs, nodes, generation = load_corpus()
    assert len(docs) == 8
    assert {d.model_code for d in docs} == {None, "HP-01", "TSHIRT-01"}
    assert all(d.revision and d.references and d.effective_status == "active" for d in docs)
    other_docs, other_nodes, other_generation = load_corpus()
    assert generation == other_generation
    assert [n.node_id for n in nodes] == [n.node_id for n in other_nodes]
    assert len({n.node_id for n in nodes}) == len(nodes)
    assert all(n.metadata["index_generation"] == generation for n in nodes)
    assert all(node_to_metadata_dict(n)["document_id"] == n.metadata["document_id"] for n in nodes)
    assert len(other_docs) == len(docs)


def test_invalid_scope_rejected_before_index(tmp_path):
    meta = dict(document_id="BAD", title="bad", revision="1", category="general",
                model_code="HP-01", effective_status="active", references=["https://example.com"])
    (tmp_path / "bad.md").write_text("---\n" + json.dumps(meta) + "\n---\n## Test\nBody", encoding="utf-8")
    with pytest.raises(ValueError):
        load_corpus(tmp_path)


def test_sparse_encoding_is_stable_and_chinese_aware():
    encoder = BM25Encoder()
    first = encoder.queries(["HP-01 右耳无声", "TSHIRT-01 尺码"])
    assert first == encoder.queries(["HP-01 右耳无声", "TSHIRT-01 尺码"])
    assert first[0][0] and set(first[0][0]) != set(first[0][1])


def test_insufficient_result_cannot_contain_sources():
    with pytest.raises(ValueError):
        RetrievalResult(query="q", model_code=None, status="insufficient_evidence", sources=[{}])


async def test_overview_can_browse_scoped_documents_without_lowering_question_threshold(monkeypatch):
    from types import SimpleNamespace

    from llama_index.core.schema import NodeWithScore

    from app.rag import retriever as module

    docs, nodes, generation = load_corpus()
    node = next(n for n in nodes if n.metadata["model_code"] == "HP-01")

    async def manifest(*args):
        return {"state": "ready", "generation": generation, "documents": [d.model_dump() for d in docs]}

    async def retrieve(bundle):
        return [NodeWithScore(node=node, score=0.2)]

    monkeypatch.setattr(module, "read_manifest", manifest)
    worker = module.KnowledgeRetriever.__new__(module.KnowledgeRetriever)
    worker.settings = SimpleNamespace(rag_search_mode="dense", rag_rerank_enabled=False,
        rag_timeout_s=2, rag_candidate_k=4, rag_top_k=4, rag_min_dense_score=0.8)
    worker.client = None
    worker.index = SimpleNamespace(as_retriever=lambda **kwargs: SimpleNamespace(aretrieve=retrieve))
    normal = await worker.retrieve("续航多久", model_code="HP-01", category="digital")
    assert normal.status == "insufficient_evidence"
    overview = await worker.retrieve("耳机相关知识", model_code="HP-01", category="digital", overview=True)
    assert overview.status == "ok" and overview.sources[0].model_code == "HP-01"
    assert "knowledge_overview" in overview.warnings
