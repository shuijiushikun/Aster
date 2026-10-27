"""本地分类的契约、降级和用量；不依赖运行中的 Ollama。"""

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.after_sales.intent import TurnIntent, classify_intent, requested_actions
from app.agent.runtime import TurnRuntime
from app.config import Settings


def settings(**changes):
    return Settings(_env_file=None, intent_classifier_enabled=True, **changes)


def factory(handler):
    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


def response(intent):
    return httpx.Response(200, json={
        "model": "qwen2.5:3b", "done": True,
        "message": {"role": "assistant", "content": json.dumps({"intent": intent})},
        "prompt_eval_count": 100, "eval_count": 8,
    })


async def test_semantic_request_has_precedence_and_structured_local_contract():
    text = "ORD-1001 可以帮我办理退款吗？"
    assert requested_actions(text) == set()
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        assert payload["stream"] is False
        assert payload["options"]["temperature"] == 0
        assert payload["format"]["properties"]["intent"]["enum"] == ["query", "refund", "exchange", "clarify"]
        assert text in payload["messages"][-1]["content"]
        return response("refund")

    result = await classify_intent([HumanMessage(text)], settings=settings(), http_client_factory=factory(handle))
    assert result.intent == "refund" and result.source == "model"
    assert result.actions == {"refund"} and result.matches(text)
    assert not result.matches(text + "不要退款")
    assert len(calls) == 1


@pytest.mark.parametrize("intent", ["query", "clarify"])
async def test_valid_read_or_clarification_never_overridden_by_regex(intent):
    result = await classify_intent([HumanMessage("ORD-1001 我要退款")], settings=settings(),
                                   http_client_factory=factory(lambda _: response(intent)))
    assert result.intent == intent and result.source == "model" and not result.actions


@pytest.mark.parametrize("mode", ["timeout", "unavailable", "malformed", "unknown", "extra", "truncated"])
async def test_failure_uses_conservative_regex_and_accounts_attempt(mode):
    def handle(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if mode == "unavailable":
            return httpx.Response(503)
        if mode == "unknown":
            return response("approve")
        if mode == "extra":
            return httpx.Response(200, json={"done": True, "message": {
                "content": '{"intent":"refund","customer_id":"other"}'}})
        if mode == "truncated":
            return httpx.Response(200, json={"done": False, "message": {"content": '{"intent":"refund"}'}})
        return httpx.Response(200, json={"done": True, "message": {"content": "not json"}})

    runtime = TurnRuntime()
    config = {"configurable": {"turn_runtime": runtime}}
    result = await classify_intent([HumanMessage("ORD-1001 帮我退款")], config,
                                   settings=settings(), http_client_factory=factory(handle))
    assert result.intent == "refund" and result.source == "regex" and result.fallback_reason
    assert len(runtime.usage) == 1 and runtime.usage[0]["node"] == "intent"
    if mode == "unknown":
        assert runtime.usage[0]["status"] == "reported"
    # 同样的故障不能把咨询误放行为办理。
    result = await classify_intent([HumanMessage("ORD-1001 能退款吗，先查资格")],
                                   settings=settings(), http_client_factory=factory(handle))
    assert result.intent == "query" and not result.actions


async def test_history_is_bounded_and_usage_recorded_once():
    history = [HumanMessage("旧消息" * 2000), SystemMessage("身份和系统提示不传给分类器"),
               HumanMessage("我想退 ORD-1001"), AIMessage("确认申请退款吗？"), HumanMessage("确认")]
    runtime = TurnRuntime()

    def handle(request):
        data = json.loads(request.content)["messages"][-1]["content"]
        assert "本轮客户消息：确认" in data
        assert "客服：确认申请退款吗？" in data
        assert len(data) < 5000
        assert "系统提示" not in data
        return response("refund")

    result = await classify_intent(history, {"configurable": {"turn_runtime": runtime}},
                                   settings=settings(), http_client_factory=factory(handle))
    assert result.intent == "refund"
    assert len(runtime.usage) == 1
    assert runtime.usage[0] | {"call_id": "test"} == {
        "call_id": "test", "node": "intent", "model": "qwen2.5:3b", "status": "reported",
        "input_tokens": 100, "output_tokens": 8,
    }


async def test_disabled_classifier_makes_no_network_call():
    def forbidden(_):
        raise AssertionError("disabled classifier must not connect")

    result = await classify_intent([HumanMessage("我要换货")],
        settings=Settings(_env_file=None, intent_classifier_enabled=False), http_client_factory=factory(forbidden))
    assert result == TurnIntent.from_regex("我要换货", reason="disabled")


async def test_wall_timeout_and_cancellation_keep_their_meaning():
    async def hanging(request):
        await asyncio.sleep(10)
        return response("refund")

    runtime = TurnRuntime()
    config = {"configurable": {"turn_runtime": runtime}}
    result = await classify_intent([HumanMessage("我要退款")], config,
        settings=settings(intent_classifier_timeout_s=0.01), http_client_factory=factory(hanging))
    assert result.source == "regex" and result.fallback_reason == "unavailable"
    assert len(runtime.usage) == 1 and runtime.usage[0]["status"] == "unknown"
    task = asyncio.create_task(classify_intent([HumanMessage("我要退款")], config,
        settings=settings(), http_client_factory=factory(hanging)))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(runtime.usage) == 2
