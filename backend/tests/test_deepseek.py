"""DeepSeek 适配合同；请求经过已安装的 ChatOpenAI 和 OpenAI SDK。"""

import json

import httpx
import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import BaseModel

from app.agent import llm
from app.config import Settings


@pytest.fixture
def settings(monkeypatch):
    configured = Settings(
        _env_file=None,
        env="dev",
        llm_provider="deepseek",
        anthropic_api_key="",
        openai_api_key="",
        gemini_api_key="",
        omniroute_api_key="",
        deepseek_api_key="test-deepseek-only",
        deepseek_base_url="https://deepseek.test",
        llm_max_retries=0,
        llm_timeout_s=7,
        llm_connect_timeout_s=2,
        max_output_tokens=128,
    )
    monkeypatch.setattr(llm, "get_settings", lambda: configured)
    return configured


@pytest.fixture
async def model_http(monkeypatch):
    """替换 HTTP 边界，保留真实模型的序列化、响应解析和流处理。"""
    clients = []
    requests = []

    def configure(handler):
        async def record(request):
            requests.append(request)
            return handler(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(record))
        clients.append(client)
        monkeypatch.setattr(
            "langchain_openai.ChatOpenAI",
            lambda **kwargs: ChatOpenAI(http_async_client=client, **kwargs),
        )
        return requests

    yield configure
    for client in clients:
        await client.aclose()


def completion(content="你好", *, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-deepseek-test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    }


LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_order",
        "description": "查询指定订单的只读状态。",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
}


class StatusReply(BaseModel):
    status: str


def test_deepseek_settings_and_primary_model(settings):
    assert settings.deepseek_model == "deepseek-flash"
    assert settings.exchange_window_days == 30
    assert settings.has_llm_key
    assert llm.primary_model_name() == "deepseek-flash"


@pytest.mark.parametrize("key", ["", "   "])
def test_deepseek_requires_its_own_key(settings, key):
    settings.deepseek_api_key = key
    settings.openai_api_key = "unrelated-key"
    assert not settings.has_llm_key
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        llm._build("deepseek")


def test_deepseek_requires_base_url(settings):
    settings.deepseek_base_url = ""
    assert not settings.has_llm_key
    with pytest.raises(RuntimeError, match="DEEPSEEK_BASE_URL"):
        llm._build("deepseek")


def test_fake_stays_available_without_deepseek_key(settings):
    from app.agent.llm_fake import ScriptedFakeChatModel

    settings.deepseek_api_key = ""
    assert isinstance(llm._build("fake"), ScriptedFakeChatModel)


async def test_deepseek_reply_uses_non_thinking_chat_completions(settings, model_http):
    requests = model_http(lambda _: httpx.Response(200, json=completion()))
    model = llm._build("deepseek")
    response = await model.ainvoke("你好")

    assert response.content == "你好"
    assert response.usage_metadata["total_tokens"] == 12
    assert requests[0].url == "https://deepseek.test/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-deepseek-only"
    body = json.loads(requests[0].content)
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 128
    assert "max_completion_tokens" not in body
    assert requests[0].extensions["timeout"] == {
        "connect": 2,
        "read": 7,
        "write": 7,
        "pool": 7,
    }


async def test_deepseek_tool_call_round_trip(settings, model_http):
    def respond(request):
        body = json.loads(request.content)
        if body["messages"][-1]["role"] == "tool":
            return httpx.Response(200, json=completion("订单已妥投"))
        return httpx.Response(
            200,
            json=completion(
                None,
                tool_calls=[
                    {
                        "id": "call-order-1",
                        "type": "function",
                        "function": {"name": "lookup_order", "arguments": '{"order_id":"o-1"}'},
                    }
                ],
            ),
        )

    requests = model_http(respond)
    model = llm._build("deepseek").bind_tools([LOOKUP_TOOL])
    question = HumanMessage(content="查询订单 o-1 的状态")
    call = await model.ainvoke([question])
    assert call.tool_calls == [
        {
            "id": "call-order-1",
            "name": "lookup_order",
            "args": {"order_id": "o-1"},
            "type": "tool_call",
        }
    ]
    response = await model.ainvoke(
        [
            question,
            call,
            ToolMessage(content='{"status":"delivered"}', tool_call_id="call-order-1"),
        ]
    )

    assert response.content == "订单已妥投"
    body = json.loads(requests[1].content)
    assert body["tools"][0]["function"]["name"] == "lookup_order"
    assert body["messages"][-1]["tool_call_id"] == "call-order-1"
    assert body["thinking"] == {"type": "disabled"}


async def test_deepseek_structured_json_mode(settings, model_http):
    requests = model_http(lambda _: httpx.Response(200, json=completion('{"status":"ok"}')))
    model = llm._build("deepseek").with_structured_output(StatusReply, method="json_mode")
    response = await model.ainvoke('返回 JSON，例如 {"status":"ok"}')

    assert response == StatusReply(status="ok")
    assert json.loads(requests[0].content)["response_format"] == {"type": "json_object"}


async def test_deepseek_invalid_json_shape_is_not_success(settings, model_http):
    model_http(lambda _: httpx.Response(200, json=completion('{"unexpected":true}')))
    model = llm._build("deepseek").with_structured_output(StatusReply, method="json_mode")
    with pytest.raises(OutputParserException):
        await model.ainvoke('返回 JSON，例如 {"status":"ok"}')


async def test_deepseek_stream_retains_final_chunk_usage(settings, model_http):
    chunks = [
        {
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "你"}, "finish_reason": None}
            ]
        },
        {"choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": None}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
        },
    ]
    events = []
    for chunk in chunks:
        chunk.update(
            id="chatcmpl-stream-test",
            object="chat.completion.chunk",
            created=0,
            model="deepseek-flash",
        )
        events.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    body = ("".join(events) + "data: [DONE]\n\n").encode()
    requests = model_http(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )
    )
    streamed = [chunk async for chunk in llm._build("deepseek").astream("你好")]

    assert "".join(chunk.content for chunk in streamed) == "你好"
    assert sum((chunk.usage_metadata or {}).get("total_tokens", 0) for chunk in streamed) == 12
    payload = json.loads(requests[0].content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(
    "status, error", [(429, RateLimitError), (401, APIStatusError), (500, APIStatusError)]
)
async def test_deepseek_http_errors_propagate_without_retry(settings, model_http, status, error):
    requests = model_http(
        lambda _: httpx.Response(
            status,
            json={"error": {"message": "test failure", "type": "api_error"}},
        )
    )
    with pytest.raises(error):
        await llm._build("deepseek", max_retries=0).ainvoke("你好")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "transport_error, sdk_error",
    [
        (httpx.ReadTimeout, APITimeoutError),
        (httpx.ConnectError, APIConnectionError),
    ],
)
async def test_deepseek_transport_errors_propagate(
    settings, model_http, transport_error, sdk_error
):
    def fail(request):
        raise transport_error("test failure", request=request)

    requests = model_http(fail)
    with pytest.raises(sdk_error):
        await llm._build("deepseek", max_retries=0).ainvoke("你好")
    assert len(requests) == 1


async def test_deepseek_respects_retry_override(settings, model_http):
    requests = model_http(
        lambda _: httpx.Response(
            429,
            headers={"retry-after-ms": "1"},
            json={"error": {"message": "test rate limit", "type": "rate_limit"}},
        )
    )
    with pytest.raises(RateLimitError):
        await llm._build("deepseek", max_retries=1).ainvoke("你好")
    assert len(requests) == 2
