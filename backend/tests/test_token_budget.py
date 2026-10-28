"""Token budgeting includes the whole prompt and protects required context."""

import pytest
import tiktoken
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent import context
from app.agent.runtime import ModelCalls, TurnRuntime, invoke_model
from app.config import get_settings


def test_english_context_uses_tokens_instead_of_character_length():
    messages = [SystemMessage("Rules"), HumanMessage("hello world " * 200)]
    assert len(messages[1].content) > 2000
    # 1,280 tokens are reserved for the reply and safety margin by default.
    result = context.fit_messages(messages, max_tokens=2000)
    assert result == messages
    assert context.context_tokens(result) <= 720


@pytest.mark.parametrize("text", ["hello world", "你好，售后退款", "😀耳机🎧", "<|endoftext|>"])
def test_text_counter_uses_bpe_and_treats_special_token_strings_as_text(text):
    from app.agent.tokens import text_tokens

    expected = len(tiktoken.get_encoding("cl100k_base").encode_ordinary(text))
    assert text_tokens(text) == expected


def test_token_truncation_preserves_unicode_and_does_not_mutate_evidence(monkeypatch):
    from app.agent.tokens import text_tokens, truncate_tokens

    text = "你好😀售后🎧 hello world " * 80
    for limit in (1, 2, 3, 7, 20, 99):
        shortened = truncate_tokens(text, limit)
        assert text.startswith(shortened)
        assert "\ufffd" not in shortened
        assert text_tokens(shortened) <= limit
    monkeypatch.setattr(get_settings(), "context_evidence_max_tokens", 20)
    evidence = {"sources": [{"document_id": "KB-1", "excerpt": text}]}
    result = context.build_context([HumanMessage("问题")], "规则", evidence=evidence)
    assert text_tokens(context.context_data(result)["evidence"]["sources"][0]["excerpt"]) <= 20
    assert evidence["sources"][0]["excerpt"] == text


def test_tools_and_tool_messages_count_but_usage_and_context_metadata_do_not():
    messages = [HumanMessage("查询"), AIMessage("", tool_calls=[
        {"name": "get_order", "args": {"order_id": "ORD-1"}, "id": "call-1"}]),
        ToolMessage('{"status":"DELIVERED"}', tool_call_id="call-1")]
    tool = {"type": "function", "function": {"name": "get_order",
        "description": "查询订单", "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "订单编号"}}}}}
    base = context.context_tokens(messages)
    assert context.context_tokens(messages, tools=[tool]) > base
    assert base > context.context_tokens([HumanMessage("查询")])
    messages[0].additional_kwargs["context_data"] = {"ignored": "x" * 10000}
    messages[1].usage_metadata = {"input_tokens": 10000, "output_tokens": 2, "total_tokens": 10002}
    assert context.context_tokens(messages) == base


async def test_oversized_tool_schema_stops_before_model_call_and_usage_record(monkeypatch):
    class Model:
        calls = 0

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage("不会执行")

    monkeypatch.setattr(get_settings(), "context_max_tokens", 2000)
    model, runtime = Model(), TurnRuntime()
    tools = [{"type": "function", "function": {"name": "read",
        "description": "tool description " * 2000,
        "parameters": {"type": "object", "properties": {}}}}]
    with pytest.raises(context.ContextBudgetExceeded):
        await ModelCalls(model).call([HumanMessage("查订单")],
            {"configurable": {"turn_runtime": runtime}}, node="agent", tools=tools)
    assert model.calls == 0
    assert runtime.usage == []


async def test_bound_legacy_model_schema_is_also_budgeted(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    async def unexpected(messages):
        raise AssertionError("oversized tools must be rejected before invoking the model")

    monkeypatch.setattr(get_settings(), "context_max_tokens", 2000)
    bound = RunnableLambda(unexpected).bind(tools=[{"type": "function", "function": {
        "name": "read", "description": "tool description " * 2000,
        "parameters": {"type": "object", "properties": {}}}}])
    with pytest.raises(context.ContextBudgetExceeded):
        await invoke_model(bound, [HumanMessage("问题")], {}, node="agent")


def test_reply_and_margin_reservations_reduce_the_available_input(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "max_output_tokens", 100)
    monkeypatch.setattr(settings, "context_token_margin", 50)
    messages = [HumanMessage("hello world " * 80)]
    size = context.context_tokens(messages)
    assert context.fit_messages(messages, max_tokens=size + 150) == messages
    with pytest.raises(context.ContextBudgetExceeded):
        context.fit_messages(messages, max_tokens=size + 149)


def test_tagged_tool_pairs_are_never_removed_as_history():
    request = AIMessage("", tool_calls=[{"name": "read", "args": {}, "id": "call"}],
                        additional_kwargs={"context_role": "history"})
    reply = ToolMessage("完整工具结果 " * 100, tool_call_id="call",
                        additional_kwargs={"context_role": "history"})
    with pytest.raises(context.ContextBudgetExceeded):
        context.fit_messages([HumanMessage("当前问题"), request, reply], max_tokens=1400)


@pytest.mark.parametrize("text, accepted", [("hello world " * 20, True), ("退款记录" * 70, False)])
async def test_summary_output_limit_uses_tokens(engine, monkeypatch, text, accepted):
    from sqlalchemy import select

    from app.agent.memory import maybe_summarize
    from app.agent.runner import create_conversation
    from app.db import session as db
    from app.db.models import ConversationSummaryRecord
    from tests.test_context import SummaryModel, add_history

    monkeypatch.setattr(get_settings(), "summary_max_tokens", 64)
    cid = await create_conversation("CUST-001")
    await add_history(cid, 16)
    model = SummaryModel(AIMessage(text))
    assert await maybe_summarize("CUST-001", cid, ModelCalls(model), {}) is accepted
    async with db.SessionLocal() as session:
        record = await session.scalar(select(ConversationSummaryRecord).where(
            ConversationSummaryRecord.conversation_id == cid))
        assert (record is not None) is accepted
    assert "64 token" in model.messages[0][0].content


def test_no_legacy_character_budget_config_is_exposed():
    settings = get_settings()
    assert settings.context_max_tokens > settings.max_output_tokens + settings.context_token_margin
    assert not hasattr(settings, "context_max_chars")
    assert not hasattr(settings, "summary_max_chars")
    # Character limits still bound HTTP/business fields, independently of model tokens.
    assert settings.max_message_chars > 0


def test_config_rejects_a_budget_with_no_input_space():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="CONTEXT_MAX_TOKENS must exceed"):
        Settings(_env_file=None, context_max_tokens=1280, max_output_tokens=1024,
                 context_token_margin=256)


async def test_legacy_graph_does_not_trip_provider_breaker_on_context_overflow(monkeypatch):
    from app.agent import graph

    class Model:
        calls = 0

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage("不应执行")

    monkeypatch.setattr(get_settings(), "context_max_tokens", 1400)
    primary, fallback = Model(), Model()
    agent = graph._compile(primary, fallback)
    with pytest.raises(context.ContextBudgetExceeded):
        await agent.ainvoke({"messages": [HumanMessage("查订单")]})
    assert primary.calls == fallback.calls == 0
    assert not graph.breaker.is_open()
