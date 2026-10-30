"""校准报告与临时测试库的执行边界；不连接真实服务。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.engine import make_url

from app.config import Settings
from app.rag.evaluate import calibrated_settings
from app.rag.sparse import SPARSE_VERSION
from evals.datasets import RETRIEVAL_PATH, fingerprint
from scripts.business_eval import disposable_database


def calibration(settings):
    return dict(split="calibration", dataset_sha256=fingerprint(RETRIEVAL_PATH), generation="knowledge-v1",
                embedding_model=settings.embed_model_name, dimension=settings.embed_dimension,
                sparse_version=SPARSE_VERSION, suggested_min_dense_score=0.7, suggested_min_bm25_score=6.1)


def test_calibration_thresholds_only_affect_the_evaluation_copy(tmp_path):
    settings = Settings(_env_file=None, rag_min_dense_score=0.8, rag_min_bm25_score=5)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calibration(settings)), encoding="utf-8")
    result = calibrated_settings(settings, path, "knowledge-v1")
    assert (result.rag_min_dense_score, result.rag_min_bm25_score) == (0.7, 6.1)
    assert (settings.rag_min_dense_score, settings.rag_min_bm25_score) == (0.8, 5)


@pytest.mark.parametrize("change", [{"split": "evaluation"}, {"dataset_sha256": "old"},
    {"generation": "old-knowledge"}, {"suggested_min_dense_score": float("nan")},
    {"suggested_min_bm25_score": -1}])
def test_mismatched_or_invalid_calibration_is_rejected(tmp_path, change):
    settings = Settings(_env_file=None)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calibration(settings) | change), encoding="utf-8")
    with pytest.raises(ValueError):
        calibrated_settings(settings, path, "knowledge-v1")


def test_temporary_database_is_cleaned_up_after_runner_failure(monkeypatch):
    connection = MagicMock()
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr("scripts.business_eval.psycopg.connect", connect)
    with pytest.raises(RuntimeError, match="runner failure"), disposable_database(
            "postgresql://test:test@localhost/demo") as url:
        temporary_name = make_url(url).database
        assert temporary_name.startswith("aster_eval_")
        raise RuntimeError("runner failure")
    commands = [call.args[0].as_string() for call in connection.execute.call_args_list]
    assert commands == [f'CREATE DATABASE "{temporary_name}" TEMPLATE template0',
                        f'DROP DATABASE "{temporary_name}" WITH (FORCE)']
    assert make_url(connect.call_args.args[0]).database == "postgres"


def test_sqlite_configuration_is_rejected_before_connecting(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr("scripts.business_eval.psycopg.connect", connect)
    with pytest.raises(ValueError, match="PostgreSQL"), disposable_database("sqlite:///existing.db"):
        pytest.fail("SQLite must not be used for the PG workflow evaluation")
    connect.assert_not_called()


async def test_parallel_evaluation_limits_requests_and_preserves_case_order(monkeypatch):
    from app.rag import evaluate as module
    from evals.datasets import load_retrieval_cases

    cases = [c for c in load_retrieval_cases() if c.split == "evaluation"]
    cases = [next(c for c in cases if c.expected_document_id), next(c for c in cases if not c.expected_document_id)]
    instances = []
    published = {"generation": "test-generation", "collection": "pinned-snapshot"}
    reads = []

    async def manifest(*args):
        reads.append(True)
        return published

    class Retriever:
        def __init__(self, *args):
            self.active = self.peak = self.batches = 0
            self.embedding = SimpleNamespace(aget_text_embedding_batch=self.embed)
            instances.append(self)

        async def embed(self, texts):
            self.batches += 1
            return [[1.0] for _ in texts]

        async def retrieve(self, *args, **kwargs):
            assert kwargs["snapshot"] is published
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.001)
            self.active -= 1
            return SimpleNamespace(sources=[], status="insufficient_evidence", reason="test", warnings=[])

    @asynccontextmanager
    async def store(settings):
        yield None, None

    monkeypatch.setattr(module, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(module, "load_retrieval_cases", lambda: cases)
    monkeypatch.setattr(module, "load_corpus", lambda: ([], [], "test-generation"))
    monkeypatch.setattr(module, "open_store", store)
    monkeypatch.setattr(module, "read_manifest", manifest)
    monkeypatch.setattr(module, "KnowledgeRetriever", Retriever)
    parallel = await module.evaluate(concurrency=2)
    sequential = await module.evaluate(concurrency=1)
    assert instances[0].peak == 2 and instances[1].peak == 1
    assert all(instance.batches == 1 for instance in instances)
    assert [(r["id"], r["mode"]) for r in parallel["rows"]] == [
        (r["id"], r["mode"]) for r in sequential["rows"]]
    assert parallel["concurrency"] == 2
    assert len(reads) == 2 and parallel["index_collection"] == "pinned-snapshot"


@pytest.mark.parametrize("concurrency", [0, 9])
async def test_invalid_evaluation_concurrency_is_rejected(concurrency):
    from app.rag.evaluate import evaluate

    with pytest.raises(ValueError, match="并发"):
        await evaluate(concurrency=concurrency)


async def test_evaluation_rejects_a_different_published_generation(monkeypatch):
    from app.rag import evaluate as module

    @asynccontextmanager
    async def store(settings):
        yield None, None

    async def manifest(*args):
        return {"generation": "another-generation"}

    monkeypatch.setattr(module, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(module, "open_store", store)
    monkeypatch.setattr(module, "KnowledgeRetriever", lambda *args: SimpleNamespace())
    monkeypatch.setattr(module, "read_manifest", manifest)
    with pytest.raises(ValueError, match="generation differs"):
        await module.evaluate(concurrency=1)


def test_answer_metrics_separate_missing_evidence_and_verification_outage():
    from scripts.answer_eval import score_answers

    rows = [
        {"expected": "SIZE", "passed": True, "answer_status": "partial", "retrieval_status": "ok"},
        {"expected": None, "passed": True, "answer_status": "insufficient", "retrieval_status": "ok"},
        {"expected": None, "passed": False, "answer_status": "unavailable", "retrieval_status": "ok"},
        {"expected": None, "passed": False, "answer_status": "supported", "retrieval_status": "ok"},
    ]
    score = score_answers(rows)
    assert score["passed"] == 2 and score["positive_answered"] == 1
    assert score["negative_abstained"] == 1 and score["negative_answered"] == 1
    assert score["verification_unavailable"] == 1 and score["retrieval_unavailable"] == 0
