"""只验证本地文档契约；真实向量库/百炼验证见 app.rag.evaluate。"""

import json

import pytest
from llama_index.core.vector_stores.utils import node_to_metadata_dict

from app.rag.corpus import load_corpus
from app.rag.schemas import RetrievalResult
from app.rag.sparse import sparse_vectors


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
    first = sparse_vectors(["HP-01 右耳无声", "TSHIRT-01 尺码"])
    assert first == sparse_vectors(["HP-01 右耳无声", "TSHIRT-01 尺码"])
    assert first[0][0] and set(first[0][0]) != set(first[0][1])


def test_insufficient_result_cannot_contain_sources():
    with pytest.raises(ValueError):
        RetrievalResult(query="q", model_code=None, status="insufficient_evidence", sources=[{}])
