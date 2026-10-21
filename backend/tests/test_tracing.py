"""Opik tracing (`app/tracing.py`): off by default, and when on, every turn is
traced, scored with its outcome, and linked from the reasoning timeline."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage

from app import tracing
from app.agent import graph as graph_module
from app.agent.prompts import prompt_version
from app.agent.runner import run_agent_turn
from app.config import get_settings
from tests.test_resilience import FakeModel, _tool_call


class FakeTracer(BaseCallbackHandler):
    """Stands in for `OpikTracer`: records that LangChain invoked it."""

    def __init__(self):
        self.chain_starts = 0

    def on_chain_start(self, *args, **kwargs):
        self.chain_starts += 1

    def created_traces(self):
        return [SimpleNamespace(id="trace-123")]


class FakeOpikClient:
    def __init__(self):
        self.scores: list[dict] = []

    def log_traces_feedback_scores(self, scores, project_name=None):
        self.scores.extend(scores)


@pytest.fixture
def opik_on(monkeypatch):
    """Tracing enabled against a fake client and tracer factory."""
    client = FakeOpikClient()
    tracers: list[tuple[FakeTracer, dict]] = []

    def factory(conversation_id, customer_id, *, scripted):
        tracer = FakeTracer()
        tracers.append(
            (tracer, {"cid": conversation_id, "customer": customer_id, "scripted": scripted})
        )
        return tracer

    monkeypatch.setattr(tracing, "_client", client)
    monkeypatch.setattr(tracing, "tracer_for_turn", factory)
    return SimpleNamespace(client=client, tracers=tracers)


def _scores(client) -> dict[str, float]:
    return {s["name"]: s["value"] for s in client.scores}


def _steps(events, step_type):
    return [e for e in events if e.get("kind") == "step" and e["step_type"] == step_type]


def _script(monkeypatch, order_id: str, reply: str):
    monkeypatch.setattr(
        graph_module,
        "get_chat_model",
        lambda: FakeModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[_tool_call("issue_refund", {"order_id": order_id}, "1")],
                ),
                AIMessage(content=reply),
            ]
        ),
    )


async def _turn(customer_id: str, text: str) -> list[dict]:
    return [e async for e in run_agent_turn(customer_id, None, text)]


async def test_tracing_is_off_without_config(monkeypatch, engine):
    assert not get_settings().opik_enabled
    assert tracing.tracer_for_turn("conv-x", "CUST-001", scripted=False) is None
    _script(monkeypatch, "ORD-1001", "Your refund has been approved.")
    events = await _turn("CUST-001", "refund ORD-1001")
    assert _steps(events, "trace") == []


async def test_turn_is_traced_scored_and_linked(monkeypatch, engine, opik_on):
    _script(monkeypatch, "ORD-1001", "Your refund has been approved.")
    events = await _turn("CUST-001", "refund ORD-1001")

    conversation_id = events[0]["conversation_id"]
    tracer, args = opik_on.tracers[0]
    assert args == {"cid": conversation_id, "customer": "CUST-001", "scripted": False}
    # LangChain actually ran the callback for the graph.
    assert tracer.chain_starts > 0

    assert _scores(opik_on.client) == {
        "refund_approved": 1.0,
        "refund_denied": 0.0,
        "refund_escalated": 0.0,
        "sanitizer_correction": 0.0,
        "fallback_used": 0.0,
        "turn_error": 0.0,
    }
    assert all(s["id"] == "trace-123" for s in opik_on.client.scores)

    (trace,) = _steps(events, "trace")
    assert trace["payload"]["trace_id"] == "trace-123"
    assert "trace_id=trace-123" in trace["payload"]["url"]


async def test_scores_capture_denial_and_correction(monkeypatch, engine, opik_on):
    # Bob's final-sale order is denied by the gate while the "model" claims success.
    _script(monkeypatch, "ORD-1002", "Your refund has been approved!")
    await _turn("CUST-002", "Please refund ORD-1002")
    scores = _scores(opik_on.client)
    assert scores["refund_denied"] == 1.0
    assert scores["refund_approved"] == 0.0
    assert scores["sanitizer_correction"] == 1.0


async def test_failed_turn_is_scored_as_error(monkeypatch, engine, opik_on):
    class Broken:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            raise RuntimeError("provider down")

    monkeypatch.setattr(graph_module, "get_chat_model", lambda: Broken())
    events = await _turn("CUST-001", "refund ORD-1001")
    assert any(e.get("kind") == "error" for e in events)
    assert _scores(opik_on.client)["turn_error"] == 1.0
    assert len(_steps(events, "trace")) == 1


async def test_demo_trace_step_has_no_workspace_link(monkeypatch, engine, opik_on):
    monkeypatch.setattr(get_settings(), "auth_mode", "demo")
    _script(monkeypatch, "ORD-1001", "Your refund has been approved.")
    events = await _turn("CUST-001", "refund ORD-1001")
    (trace,) = _steps(events, "trace")
    assert trace["payload"] == {"trace_id": "trace-123"}


def test_tracer_carries_thread_tags_and_metadata(monkeypatch):
    import opik.integrations.langchain as opik_langchain

    captured = {}

    class RecordingTracer:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(tracing, "_client", object())
    monkeypatch.setattr(opik_langchain, "OpikTracer", RecordingTracer)
    assert tracing.tracer_for_turn("conv-1", "CUST-009", scripted=True) is not None
    assert captured["thread_id"] == "conv-1"
    assert captured["project_name"] == get_settings().opik_project_name
    assert f"prompt:{prompt_version()}" in captured["tags"]
    assert "scripted" in captured["tags"] and "provider:fake" in captured["tags"]
    assert captured["metadata"]["customer_id"] == "CUST-009"
    assert captured["opik_context_read_only_mode"] is True


def test_tracing_failures_never_break_a_turn(monkeypatch):
    class Exploding:
        def created_traces(self):
            raise RuntimeError("opik backend unreachable")

    assert tracing.finish_turn(Exploding(), tracing.TurnOutcome()) is None
    assert tracing.finish_turn(None, tracing.TurnOutcome()) is None


def test_enabling_opik_keeps_the_apps_sentry_client():
    """`import opik` re-initialises Sentry with Comet's DSN unless told not to.
    The SDK skips that under pytest, so check in a clean interpreter."""
    import os
    import subprocess
    import sys

    script = (
        "import sentry_sdk\n"
        "from app.observability import configure_sentry\n"
        "from app import tracing\n"
        "configure_sentry()\n"
        "tracing.configure()\n"
        "import opik\n"
        "print(sentry_sdk.get_client().dsn)\n"
    )
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("OPIK_", "PYTEST"))
    } | {
        "OPIK_URL_OVERRIDE": "http://127.0.0.1:9/api",
        "SENTRY_DSN": "https://abc@o1.ingest.sentry.io/1",
    }
    out = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "https://abc@o1.ingest.sentry.io/1"


def test_anthropic_cache_tokens_are_billed_once():
    """Opik maps LangChain's cache-inclusive `input_tokens` onto Anthropic's
    cache-exclusive field; the shim makes cached tokens count once."""
    from opik import llm_usage
    from opik.integrations.langchain.provider_usage_extractors.langchain_run_helpers import (
        langchain_usage,
    )

    assert tracing.fix_anthropic_cache_accounting()
    lc = langchain_usage.LangChainUsage.from_original_usage_dict(
        {
            # As langchain-anthropic reports it: 2400 fresh + 1800 cache-read input.
            "input_tokens": 4200,
            "output_tokens": 120,
            "total_tokens": 4320,
            "input_token_details": {"cache_read": 1800, "cache_creation": 0},
        }
    )
    usage = llm_usage.OpikUsage.from_anthropic_dict(lc.map_to_anthropic_usage())
    assert usage.prompt_tokens == 4200  # not 6000
    assert usage.provider_usage.input_tokens == 2400
    assert usage.provider_usage.cache_read_input_tokens == 1800
    # Idempotent.
    assert tracing.fix_anthropic_cache_accounting()
    assert lc.map_to_anthropic_usage()["input_tokens"] == 2400


def test_hosted_opik_ignores_a_local_config_file(monkeypatch):
    """An API key without OPIK_URL_OVERRIDE must go to Comet, even when
    ~/.opik.config points at a self-hosted instance."""
    from opik.config import OPIK_URL_CLOUD

    s = get_settings()
    monkeypatch.setattr(s, "opik_api_key", "k")
    monkeypatch.setattr(s, "opik_url_override", "")
    assert tracing.opik_host() == OPIK_URL_CLOUD
    monkeypatch.setattr(s, "opik_url_override", "http://localhost:5173/api")
    assert tracing.opik_host() == "http://localhost:5173/api"
