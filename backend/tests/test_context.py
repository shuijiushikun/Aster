"""Context budgets, owned cross-session facts and incremental summary coverage."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import select

from app.agent import context, graph
from app.agent.runner import create_conversation, run_agent_turn
from app.agent.runtime import ModelCalls, TurnRuntime
from app.config import get_settings
from app.db import session as db
from app.db.models import AfterSalesTicket, Conversation, ConversationSummaryRecord, Message
from tests.seed_data import seed_exchange_samples


def test_assembly_keeps_current_once_and_history_in_order_and_data_out_of_system():
    messages = [HumanMessage("旧问题"), AIMessage("旧回答"), HumanMessage("当前问题")]
    result = context.assemble(messages, "节点职责", evidence={"sources": [{"excerpt": "检索正文片段"}]})
    assert [m.content for m in result if m.content in ("旧问题", "旧回答", "当前问题")] == [
        "旧问题", "旧回答", "当前问题"
    ]
    assert sum(m.content == "当前问题" for m in result) == 1
    assert "检索正文片段" not in result[0].content
    assert isinstance(result[0], SystemMessage)
    assert isinstance(result[-1], HumanMessage)


def test_budget_drops_old_history_then_summary_then_evidence_and_preserves_core():
    messages = [HumanMessage("旧" * 900), AIMessage("答" * 900), HumanMessage("保留当前问题")]
    result = context.build_context(
        messages, "安全规则", max_tokens=2180,
        facts={"owner_customer_id": "CUST-1", "ticket": {"id": "TKT-1", "status": "PENDING_REVIEW"}},
        summaries=[{"summary_text": "摘要" * 600}],
        evidence={"sources": [{"document_id": "KB-1", "excerpt": "证据" * 600}]},
    )
    assert context.context_tokens(result) <= 900
    text = "".join(str(m.content) for m in result)
    assert "CUST-1" in text and "PENDING_REVIEW" in text and "保留当前问题" in text
    assert "旧" * 10 not in text and "摘要" * 10 not in text and "KB-1" not in text


def test_mandatory_overflow_fails_and_tool_pairs_stay_ordered():
    with pytest.raises(context.ContextBudgetExceeded):
        context.build_context([HumanMessage("当前" * 1000)], "规则", max_tokens=1380)
    request = AIMessage("", tool_calls=[{"name": "read", "args": {}, "id": "call"}])
    reply = ToolMessage("结果", tool_call_id="call")
    messages = [SystemMessage("规则"), HumanMessage("当前"), request, reply]
    assert context.fit_messages(messages, max_tokens=2280) == messages


async def add_history(cid, count):
    gap_cid = await create_conversation("CUST-002")
    ids = []
    async with db.SessionLocal() as session:
        for index in range(count):
            message = Message(conversation_id=cid, role="user" if index % 2 == 0 else "assistant",
                              content=f"原文-{index}")
            session.add(message)
            await session.flush()
            ids.append(message.id)
            session.add(Message(conversation_id=gap_cid, role="user", content="其他会话"))
            await session.flush()
        await session.commit()
    return ids


class SummaryModel:
    def __init__(self, result=None):
        self.result = result or AIMessage("任务摘要：客户咨询耳机故障，需核实审批事实。",
            usage_metadata={"input_tokens": 17, "output_tokens": 3, "total_tokens": 20})
        self.messages = []

    async def ainvoke(self, messages):
        self.messages.append(messages)
        if isinstance(self.result, Exception):
            raise self.result
        if self.result == "timeout":
            await asyncio.sleep(1)
        return self.result


async def test_summary_threshold_real_ids_incremental_coverage_and_no_duplicate_history(engine):
    from app.agent.memory import load_context, maybe_summarize

    cid = await create_conversation("CUST-001")
    model, runtime = SummaryModel(), TurnRuntime()
    config = {"configurable": {"turn_runtime": runtime}}
    ids = await add_history(cid, 14)
    assert not await maybe_summarize("CUST-001", cid, ModelCalls(model), config)
    extra = await add_history(cid, 2)
    assert await maybe_summarize("CUST-001", cid, ModelCalls(model), config)
    async with db.SessionLocal() as session:
        record = await session.get(ConversationSummaryRecord, cid)
        assert record.covered_message_id == ids[7] != 8
    assert len(model.messages) == len(runtime.usage) == 1
    source = "".join(str(m.content) for m in model.messages[0])
    assert "原文-7" in source and "原文-8" not in source
    assembled = await load_context("CUST-001", cid, current_message_id=extra[0])
    assert assembled[-1].content == "原文-0"
    assert all("原文-7" != m.content for m in assembled)
    assert len([m for m in assembled if m.additional_kwargs.get("context_role") == "history"]) == 6
    assert not await maybe_summarize("CUST-001", cid, ModelCalls(model), config)


@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), "timeout", AIMessage("")])
async def test_summary_failure_keeps_pointer_and_accounts_attempt(engine, monkeypatch, failure):
    from app.agent.memory import maybe_summarize

    monkeypatch.setattr(get_settings(), "summary_timeout_s", 0.03)
    cid = await create_conversation("CUST-001")
    await add_history(cid, 16)
    runtime, model = TurnRuntime(), SummaryModel(failure)
    assert not await maybe_summarize("CUST-001", cid, ModelCalls(model),
                                    {"configurable": {"turn_runtime": runtime}})
    async with db.SessionLocal() as session:
        assert await session.get(ConversationSummaryRecord, cid) is None
    assert len(runtime.usage) == 1
    if not isinstance(failure, AIMessage):
        assert runtime.usage[0]["status"] == "unknown"


async def test_long_summary_prefix_advances_a_bounded_contiguous_batch(engine, monkeypatch):
    from app.agent.memory import maybe_summarize

    monkeypatch.setattr(get_settings(), "context_max_tokens", 2480)
    cid = await create_conversation("CUST-001")
    await add_history(cid, 16)
    async with db.SessionLocal() as session:
        rows = list(await session.scalars(select(Message).where(Message.conversation_id == cid)
                                          .order_by(Message.id)))
        for row in rows:
            row.content = "长消息" * 160
        await session.commit()
        first_ids = [row.id for row in rows[:8]]
    assert await maybe_summarize("CUST-001", cid, ModelCalls(SummaryModel()),
                                {"configurable": {"turn_runtime": TurnRuntime()}})
    async with db.SessionLocal() as session:
        record = await session.get(ConversationSummaryRecord, cid)
        assert first_ids[0] <= record.covered_message_id < first_ids[-1]


async def test_context_only_loads_owned_related_summary_and_does_not_resume(engine, monkeypatch):
    from app.agent.memory import load_context

    await seed_exchange_samples()
    own = await create_conversation("CUST-EX-001")
    alien = await create_conversation("CUST-EX-002")
    unrelated = await create_conversation("CUST-EX-001")
    fresh = await create_conversation("CUST-EX-001")
    async with db.SessionLocal() as session:
        for cid, owner, text in [(own, "CUST-EX-001", "本人同工单摘要"),
                                 (alien, "CUST-EX-002", "其他客户秘密"),
                                 (unrelated, "CUST-EX-001", "无关会话摘要")]:
            msg = Message(conversation_id=cid, role="user", content=text)
            session.add(msg)
            await session.flush()
            session.add(ConversationSummaryRecord(conversation_id=cid, customer_id=owner,
                        summary_text=text, covered_message_id=msg.id))
        session.add(AfterSalesTicket(id="TKT-OWN", customer_id="CUST-EX-001",
                    order_id="ORD-EX-1001", origin_conversation_id=own,
                    workflow_thread_id="exchange:TKT-OWN", status="PENDING_REVIEW", description="故障"))
        session.add(AfterSalesTicket(id="TKT-ALIEN", customer_id="CUST-EX-002",
                    order_id="ORD-EX-2001", origin_conversation_id=alien,
                    workflow_thread_id="exchange:TKT-ALIEN", status="PENDING_REVIEW", description="秘密"))
        current = Message(conversation_id=fresh, role="user", content="TKT-OWN 现在什么进度")
        session.add(current)
        await session.commit()
        current_id = current.id
    messages = await load_context("CUST-EX-001", fresh, current_message_id=current_id)
    text = "".join(str(m.content) for m in messages)
    assert "本人同工单摘要" in text and "PENDING_REVIEW" in text and "ORD-EX-1001" in text
    assert "其他客户秘密" not in text and "无关会话摘要" not in text
    assert all("本人同工单摘要" not in m.content for m in messages if isinstance(m, SystemMessage))
    with pytest.raises(PermissionError):
        await load_context("CUST-EX-002", fresh, current_message_id=current_id)


async def test_runner_summary_usage_is_counted_once_and_failure_does_not_fail_chat(engine, monkeypatch):
    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            if any("增量摘要" in str(m.content) for m in messages if isinstance(m, SystemMessage)):
                raise RuntimeError("summary unavailable")
            return AIMessage("回复", usage_metadata={"input_tokens": 10, "output_tokens": 2,
                                                     "total_tokens": 12})

    monkeypatch.setattr(graph, "get_chat_model", lambda: Model())
    monkeypatch.setattr(graph, "get_fallback_model", lambda: None)
    cid = await create_conversation("CUST-001")
    await add_history(cid, 14)
    events = [e async for e in run_agent_turn("CUST-001", cid, "继续")]
    assert events[-1] == {"kind": "done"}
    usage = [e for e in events if e.get("step_type") == "usage"]
    assert len(usage) == 2 and usage[-1]["node"] == "summary"
    assert usage[-1]["payload"]["status"] == "unknown"
    async with db.SessionLocal() as session:
        assert (await session.get(Conversation, cid)).tokens_used == 12
        assert await session.get(ConversationSummaryRecord, cid) is None


@pytest.mark.parametrize("global_cap, expected_calls", [(12, 0), (0, 1)])
async def test_summary_rechecks_global_budget_and_bounds_preparation(
    engine, monkeypatch, global_cap, expected_calls
):
    from app.agent import memory

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage("回复", usage_metadata={"input_tokens": 10, "output_tokens": 2,
                                                     "total_tokens": 12})

    calls = []
    cancelled = asyncio.Event()

    async def slow_summary(*args, **kwargs):
        calls.append(True)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("summary preparation must have timed out")

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "demo_global_daily_token_budget", global_cap)
    # 超时也覆盖数据库准备；给事务校验和任务调度留出余量。
    monkeypatch.setattr(settings, "summary_timeout_s", 0.5)
    monkeypatch.setattr(graph, "get_chat_model", lambda: Model())
    monkeypatch.setattr(graph, "get_fallback_model", lambda: None)
    monkeypatch.setattr(memory, "maybe_summarize", slow_summary)
    cid = await create_conversation("CUST-001")
    events = [e async for e in run_agent_turn("CUST-001", cid, "继续")]
    assert events[-1] == {"kind": "done"}
    assert len(calls) == expected_calls
    assert cancelled.is_set() == bool(expected_calls)
