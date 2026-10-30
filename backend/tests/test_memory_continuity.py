"""摘要覆盖位置与原文连续衔接，超预算时经受控摘要后再调用模型。"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select

from app import redis as shared
from app.agent import context
from app.agent.memory import load_context, maybe_summarize
from app.agent.runner import create_conversation, run_agent_turn
from app.agent.runtime import ModelCalls, TurnRuntime
from app.config import get_settings
from app.db import session as db
from app.db.models import Conversation, ConversationSummaryRecord, Message


async def history(cid, count, *, start=1, detail=""):
    ids = []
    async with db.SessionLocal() as session:
        for number in range(start, start + count):
            row = Message(conversation_id=cid, role="user" if number % 2 else "assistant",
                          content=f"M{number}: {detail}")
            session.add(row)
            await session.flush()
            ids.append(row.id)
        await session.commit()
    return ids


class MemoryModel:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        summary = any("增量摘要" in str(message.content) for message in messages)
        self.calls.append(("summary" if summary else "agent", messages))
        if summary:
            if isinstance(self.failure, Exception):
                raise self.failure
            if self.failure == "timeout":
                await asyncio.sleep(1)
            if isinstance(self.failure, AIMessage):
                return self.failure
        return AIMessage("已归纳历史问题和待办事项。" if summary else "已收到。",
                         usage_metadata={"input_tokens": 17, "output_tokens": 3, "total_tokens": 20})


@pytest.mark.parametrize("current_number", [17, 19, 23])
async def test_all_uncovered_messages_survive_between_summary_updates(engine, current_number):
    cid = await create_conversation("CUST-001")
    ids = await history(cid, 16)
    assert await maybe_summarize("CUST-001", cid, ModelCalls(MemoryModel()), {})
    ids += await history(cid, current_number - 16, start=17)
    messages = await load_context("CUST-001", cid, current_message_id=ids[-1], system_prompt="规则")
    actual = [message.id for message in messages if message.additional_kwargs.get("context_role") == "history"]
    assert actual == [f"db:{message_id}" for message_id in ids[8:-1]]
    assert messages[-1].id == f"db:{ids[-1]}"
    async with db.SessionLocal() as session:
        assert (await session.get(ConversationSummaryRecord, cid)).covered_message_id == ids[7]


def test_context_assembly_has_no_seven_message_window():
    raw = [HumanMessage(str(i)) if i % 2 else AIMessage(str(i)) for i in range(1, 14)]
    messages = context.build_context(raw, "规则")
    assert [message.content for message in messages[2:]] == [str(i) for i in range(1, 14)]


async def long_context(monkeypatch):
    monkeypatch.setattr(get_settings(), "context_max_tokens", 2400)
    cid = await create_conversation("CUST-001")
    ids = await history(cid, 12, detail="important order detail " * 32)
    ids += await history(cid, 1, start=13, detail="CURRENT question")
    messages = await load_context("CUST-001", cid, current_message_id=ids[-1], system_prompt="规则")
    return cid, ids, messages


async def test_budget_summary_covers_prefix_and_reuses_it_on_next_model_call(engine, monkeypatch):
    cid, ids, messages = await long_context(monkeypatch)
    runtime, model = TurnRuntime(), MemoryModel()
    config = {"configurable": {"turn_runtime": runtime}}
    caller = ModelCalls(model)
    await caller.call(messages, config, node="agent")
    async with db.SessionLocal() as session:
        record = await session.get(ConversationSummaryRecord, cid)
        assert record and record.covered_message_id < ids[-1]
    prepared = model.calls[-1][1]
    retained = [m.id for m in prepared if m.additional_kwargs.get("context_role") == "history"]
    assert retained == [f"db:{mid}" for mid in ids[:-1] if mid > record.covered_message_id]
    assert prepared[-1].id == f"db:{ids[-1]}"
    assert context.context_tokens(prepared) <= context.input_token_limit()
    assert "CURRENT question" not in "".join(str(m.content) for m in model.calls[0][1])
    summaries = sum(node == "summary" for node, _ in model.calls)
    await caller.call(messages, config, node="agent")
    assert sum(node == "summary" for node, _ in model.calls) == summaries
    assert len(runtime.usage) == len(model.calls)
    assert runtime.usage[-1]["node"] == "agent"


@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), "timeout", AIMessage("")])
async def test_required_history_overflow_fails_without_advancing_coverage(engine, monkeypatch, failure):
    cid, ids, messages = await long_context(monkeypatch)
    if failure == "timeout":
        monkeypatch.setattr(get_settings(), "summary_timeout_s", 0.1)
    runtime, model = TurnRuntime(), MemoryModel(failure)
    with pytest.raises(context.ContextBudgetExceeded):
        await ModelCalls(model).call(messages, {"configurable": {"turn_runtime": runtime}}, node="agent")
    assert model.calls and all(node == "summary" for node, _ in model.calls)
    assert len(runtime.usage) == 1 and runtime.usage[0]["node"] == "summary"
    async with db.SessionLocal() as session:
        assert await session.get(ConversationSummaryRecord, cid) is None
        assert list(await session.scalars(select(Message.id).where(Message.conversation_id == cid)
                                          .order_by(Message.id))) == ids


async def test_failed_compaction_preserves_an_existing_summary(engine, monkeypatch):
    cid = await create_conversation("CUST-001")
    ids = await history(cid, 16)
    assert await maybe_summarize("CUST-001", cid, ModelCalls(MemoryModel()), {})
    async with db.SessionLocal() as session:
        record = await session.get(ConversationSummaryRecord, cid)
        old_text, old_pointer = record.summary_text, record.covered_message_id
        rows = list(await session.scalars(select(Message).where(Message.id.in_(ids[8:]))))
        for row in rows:
            row.content += "important order detail " * 50
        await session.commit()
    current = (await history(cid, 1, start=17))[0]
    monkeypatch.setattr(get_settings(), "context_max_tokens", 2400)
    messages = await load_context("CUST-001", cid, current_message_id=current, system_prompt="规则")
    with pytest.raises(context.ContextBudgetExceeded):
        await ModelCalls(MemoryModel(RuntimeError("unavailable"))).call(messages, {}, node="agent")
    async with db.SessionLocal() as session:
        record = await session.get(ConversationSummaryRecord, cid)
        assert (record.summary_text, record.covered_message_id) == (old_text, old_pointer)


async def test_budget_summary_uses_fallback_without_duplicate_accounting(engine, monkeypatch):
    _, _, messages = await long_context(monkeypatch)
    runtime = TurnRuntime()
    primary, fallback = MemoryModel(RuntimeError("primary unavailable")), MemoryModel()
    await ModelCalls(primary, fallback).call(messages, {"configurable": {"turn_runtime": runtime}}, node="agent")
    assert primary.calls[0][0] == "summary"
    assert fallback.calls[-1][0] == "agent"
    assert len(runtime.usage) == len(primary.calls) + len(fallback.calls)
    assert runtime.usage[0]["status"] == "unknown"


@pytest.mark.parametrize("second_batch_fails", [False, True])
async def test_large_backlog_is_bounded_and_keeps_successful_coverage(engine, monkeypatch, second_batch_fails):
    monkeypatch.setattr(get_settings(), "context_max_tokens", 2400)
    cid = await create_conversation("CUST-001")
    ids = await history(cid, 40, detail="important order detail " * 32)
    current = (await history(cid, 1, start=41))[0]
    messages = await load_context("CUST-001", cid, current_message_id=current, system_prompt="规则")

    class PartialModel(MemoryModel):
        async def ainvoke(self, messages):
            if second_batch_fails and self.calls:
                self.failure = RuntimeError("second batch unavailable")
            return await super().ainvoke(messages)

    model = PartialModel()
    with pytest.raises(context.ContextBudgetExceeded):
        await ModelCalls(model).call(messages, {}, node="agent")
    assert [node for node, _ in model.calls] == ["summary", "summary"]
    async with db.SessionLocal() as session:
        record = await session.get(ConversationSummaryRecord, cid)
        assert ids[0] <= record.covered_message_id < ids[-1] < current
        assert len(list(await session.scalars(select(Message.id).where(Message.conversation_id == cid)))) == 41
    if not second_batch_fails:
        await ModelCalls(model).call(messages, {}, node="agent")
        assert model.calls[-1][0] == "agent"
        async with db.SessionLocal() as session:
            record = await session.get(ConversationSummaryRecord, cid)
        actual = [m.id for m in model.calls[-1][1] if m.additional_kwargs.get("context_role") == "history"]
        assert actual == [f"db:{mid}" for mid in ids if mid > record.covered_message_id]


async def test_tool_schema_can_trigger_summary_when_plain_context_fits(engine, monkeypatch):
    cid = await create_conversation("CUST-001")
    await history(cid, 12, detail="important order detail " * 8)
    current = (await history(cid, 1, start=13))[0]
    messages = await load_context("CUST-001", cid, current_message_id=current, system_prompt="规则")
    tool = {"type": "function", "function": {"name": "read_order", "description": "read order details " * 80,
            "parameters": {"type": "object", "properties": {}}}}
    settings = get_settings()
    limit = context.context_tokens(messages, tools=[tool]) - 20
    monkeypatch.setattr(settings, "context_max_tokens", limit + settings.max_output_tokens + settings.context_token_margin)
    assert context.context_tokens(messages) < limit
    with pytest.raises(context.ContextBudgetExceeded):
        context.fit_messages(messages, tools=[tool])
    model = MemoryModel()
    await ModelCalls(model).call(messages, {}, node="agent", tools=[tool])
    assert any(node == "summary" for node, _ in model.calls)
    assert context.context_tokens(model.calls[-1][1], tools=[tool]) <= limit


async def test_summary_refresh_keeps_inflight_tool_pair_and_current_message(engine, monkeypatch):
    _, ids, messages = await long_context(monkeypatch)
    request = AIMessage("", tool_calls=[{"name": "read", "args": {}, "id": "call-1"}],
                        additional_kwargs={"context_role": "history"})
    reply = ToolMessage("TOOL RESULT", tool_call_id="call-1", additional_kwargs={"context_role": "history"})
    model = MemoryModel()
    await ModelCalls(model).call(messages + [request, reply], {}, node="agent")
    prepared = model.calls[-1][1]
    assert prepared[-2:] == [request, reply]
    assert sum(m.id == f"db:{ids[-1]}" for m in prepared) == 1
    assert all("TOOL RESULT" not in "".join(str(m.content) for m in source)
               for node, source in model.calls if node == "summary")


def test_current_summary_and_uncovered_history_are_required():
    messages = context.build_context([HumanMessage("历史"), AIMessage("回复"), HumanMessage("当前")], "规则",
        memory={"customer_id": "CUST-001", "conversation_id": "CID", "current_message_id": 3},
        summaries=[{"conversation_id": "OTHER", "summary_text": "关联摘要"},
                   {"conversation_id": "CID", "summary_text": "当前摘要" * 300, "covered_message_id": 1}])
    with pytest.raises(context.ContextBudgetExceeded):
        context.fit_messages(messages, max_tokens=1800)
    assert len(context.context_data(messages)["summaries"]) == 2


@pytest.mark.parametrize("quota", ["conversation", "customer", "global", "pending"])
async def test_forced_summary_respects_token_quotas(engine, monkeypatch, quota):
    cid, _, messages = await long_context(monkeypatch)
    settings, runtime = get_settings(), TurnRuntime()
    if quota == "conversation":
        async with db.SessionLocal() as session:
            (await session.get(Conversation, cid)).tokens_used = settings.max_conversation_tokens
            await session.commit()
    elif quota == "customer":
        await shared.add_customer_tokens("CUST-001", settings.customer_daily_token_budget)
    else:
        monkeypatch.setattr(settings, "auth_mode", "demo")
        monkeypatch.setattr(settings, "demo_global_daily_token_budget", 5)
        if quota == "global":
            await shared.add_customer_tokens("CUST-002", 5)
        else:
            runtime.usage.append({"input_tokens": 6, "output_tokens": 0})
    model = MemoryModel()
    with pytest.raises(context.ContextBudgetExceeded):
        await ModelCalls(model).call(messages, {"configurable": {"turn_runtime": runtime}}, node="agent")
    assert not model.calls


async def test_runner_explains_history_compaction_failure(engine, monkeypatch):
    from app.agent import graph

    cid, _, _ = await long_context(monkeypatch)
    model = MemoryModel(RuntimeError("summary unavailable"))
    monkeypatch.setattr(graph, "get_chat_model", lambda: model)
    monkeypatch.setattr(graph, "get_fallback_model", lambda: None)
    events = [event async for event in run_agent_turn("CUST-001", cid, "继续")]
    assert events[-1]["kind"] == "error" and "聊天记录已保留" in events[-1]["message"]
    assert all(node != "agent" for node, _ in model.calls)
