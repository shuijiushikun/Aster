"""模拟锁过期后新轮次接手，验证旧轮次在真实事务入口被拒绝。"""

import asyncio

import pytest
from sqlalchemy import func, select, update

from app import redis as shared
from app.after_sales.refund_service import RefundService
from app.agent.runner import _save_message, create_conversation
from app.db import session as db
from app.db.models import Conversation, Message, Refund, RefundConfirmation
from app.db.turns import StaleTurn, TurnStamp, current_turn, turn_scope


async def start():
    cid = await create_conversation("CUST-001")
    mid = await _save_message(cid, "user", "请处理我的订单")
    return TurnStamp(cid, mid)


@pytest.mark.parametrize("write", ["message", "event_counter", "refund"])
async def test_previous_turn_cannot_write_after_a_new_message(engine, write):
    stamp = await start()
    await _save_message(stamp.conversation_id, "user", "先不要处理了")
    with turn_scope(stamp), pytest.raises(StaleTurn):
        if write == "message":
            await _save_message(stamp.conversation_id, "assistant", "过期结果")
        elif write == "event_counter":
            async with db.SessionLocal() as session, session.begin():
                await session.execute(update(Conversation).where(Conversation.id == stamp.conversation_id)
                                      .values(event_seq=99))
        else:
            await RefundService().execute("CUST-001", "ORD-1001", stamp.conversation_id, request_id="stale")
    assert current_turn() is None
    async with db.SessionLocal() as session:
        assert (await session.get(Conversation, stamp.conversation_id)).event_seq == 0
        assert await session.scalar(select(func.count()).select_from(Refund)) == 0
        assert await session.scalar(select(func.count()).select_from(Message)
                                    .where(Message.conversation_id == stamp.conversation_id)) == 2


async def test_expired_lock_cannot_start_a_new_turn_or_delete_the_new_lock(engine):
    stamp = await start()
    old = await shared.acquire_turn_lock(stamp.conversation_id, 10)
    await shared.get_redis().delete(f"lock:conv:{stamp.conversation_id}")
    new = await shared.acquire_turn_lock(stamp.conversation_id, 10)
    with pytest.raises(StaleTurn):
        await _save_message(stamp.conversation_id, "user", "旧请求恢复", lock_token=old)
    await shared.release_turn_lock(stamp.conversation_id, old)
    assert await shared.owns_turn_lock(stamp.conversation_id, new)


async def test_new_message_waits_for_the_old_write_transaction(engine):
    stamp = await start()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def old_write():
        with turn_scope(stamp):
            async with db.SessionLocal() as session, session.begin():
                await session.scalar(select(Conversation.id).where(Conversation.id == stamp.conversation_id))
                session.add(Message(conversation_id=stamp.conversation_id, role="assistant", content="接手前完成"))
                entered.set()
                await finish.wait()

    old = asyncio.create_task(old_write())
    await entered.wait()
    new = asyncio.create_task(_save_message(stamp.conversation_id, "user", "新请求"))
    try:
        await asyncio.sleep(0.05)
        assert not new.done()
    finally:
        finish.set()
    await old
    mid = await new
    with turn_scope(stamp), pytest.raises(StaleTurn):
        await _save_message(stamp.conversation_id, "assistant", "接手后应拒绝")
    with turn_scope(TurnStamp(stamp.conversation_id, mid)):
        await _save_message(stamp.conversation_id, "assistant", "新轮次完成")


async def test_mcp_rechecks_version_at_the_actual_write_transaction(mcp_client, monkeypatch):
    from app.commerce_mcp import operations
    from tests.test_commerce_mcp import ALICE, turn_context

    commerce, _, _ = mcp_client
    context = await turn_context("请给 ORD-EX-1001 退款")
    cid = context["conversation_id"]
    authorized, resume = asyncio.Event(), asyncio.Event()
    original = operations.authorize_write

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        authorized.set()
        await resume.wait()
        return result

    monkeypatch.setattr(operations, "authorize_write", paused)
    old = asyncio.create_task(commerce.call(ALICE, "issue_refund", {"order_id": "ORD-EX-1001"},
                                              turn_context=context))
    await asyncio.wait_for(authorized.wait(), 5)
    try:
        await _save_message(cid, "user", "改为只查询订单，先不要退款")
    finally:
        resume.set()
    result = await old
    assert not result.ok and result.error.code == "ACTION_NOT_REQUESTED"
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RefundConfirmation)) == 0
        assert await session.scalar(select(func.count()).select_from(Refund)) == 0


async def test_generator_close_restores_context(engine):
    from app.agent.runner import run_agent_turn

    stream = run_agent_turn("CUST-001", None, "查看订单")
    await anext(stream)
    assert current_turn() is not None
    await stream.aclose()
    assert current_turn() is None


async def test_summary_cannot_hide_a_stale_turn_as_an_optional_failure(engine):
    from langchain_core.messages import AIMessage

    from app.agent.memory import maybe_summarize
    from app.db.models import ConversationSummaryRecord
    from tests.test_context import add_history

    cid = await create_conversation("CUST-001")
    await add_history(cid, 16)
    mid = await _save_message(cid, "user", "旧轮次")
    paused, resume = asyncio.Event(), asyncio.Event()

    class Caller:
        async def call(self, *args, **kwargs):
            paused.set()
            await resume.wait()
            return AIMessage(content="过期轮次的摘要")

    async def summarize():
        with turn_scope(TurnStamp(cid, mid)):
            return await maybe_summarize("CUST-001", cid, Caller(), {})

    old = asyncio.create_task(summarize())
    try:
        await asyncio.wait_for(paused.wait(), 15)
        await _save_message(cid, "user", "新轮次")
    finally:
        resume.set()
    with pytest.raises(StaleTurn):
        await old
    async with db.SessionLocal() as session:
        assert await session.get(ConversationSummaryRecord, cid) is None


async def test_http_old_model_reply_is_discarded_after_lock_expiry(client, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    from app.agent import graph
    from app.db.models import ReasoningEvent
    from tests.conftest import auth
    from tests.test_guardrails import _sse_events
    from tests.test_resilience import FakeModel

    paused, resume = asyncio.Event(), asyncio.Event()

    class Model(FakeModel):
        async def ainvoke(self, messages):
            user = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
            if "旧请求" in user:
                paused.set()
                await resume.wait()
                return AIMessage(content="旧结果不应入库")
            return AIMessage(content="新轮次已经完成")

    monkeypatch.setattr(graph, "get_chat_model", lambda: Model([]))
    cid = await create_conversation("CUST-001")
    old = asyncio.create_task(client.post("/api/chat", json={"conversation_id": cid, "message": "旧请求"},
                                         headers=auth("CUST-001")))
    try:
        await asyncio.wait_for(paused.wait(), 15)
        # 等价于固定 TTL 到期，不实际等待 150 秒。
        await shared.get_redis().delete(f"lock:conv:{cid}")
        new = await client.post("/api/chat", json={"conversation_id": cid, "message": "新请求"},
                                headers=auth("CUST-001"))
        assert new.status_code == 200
        assert any(e.get("content") == "新轮次已经完成" for e in _sse_events(new.text))
        async with db.SessionLocal() as session:
            sequence = (await session.get(Conversation, cid)).event_seq
    finally:
        resume.set()
        response = await asyncio.wait_for(old, 15)
    events = _sse_events(response.text)
    assert any("本轮处理已失效" in e.get("message", "") for e in events)
    assert not any(e.get("content") == "旧结果不应入库" for e in events)
    async with db.SessionLocal() as session:
        assert (await session.get(Conversation, cid)).event_seq == sequence
        assert await session.scalar(select(func.count()).select_from(Message)
            .where(Message.conversation_id == cid, Message.content == "旧结果不应入库")) == 0
        sequences = list(await session.scalars(select(ReasoningEvent.seq)
            .where(ReasoningEvent.conversation_id == cid).order_by(ReasoningEvent.seq)))
        assert sequences == list(range(1, sequence + 1))
    assert await shared.turns_in_flight() == 0


async def test_expired_confirmation_request_cannot_act_with_an_old_redis_lock(engine):
    from app.after_sales.refund_confirmation import RefundConfirmationService
    from tests.test_refund_confirmation import prepare

    cid, _, proposal = await prepare()
    old = await shared.acquire_turn_lock(cid, 10)
    await shared.get_redis().delete(f"lock:conv:{cid}")
    new = await shared.acquire_turn_lock(cid, 10)
    with pytest.raises(StaleTurn):
        await RefundConfirmationService().decide("CUST-001", cid, proposal["confirmation"]["id"],
                                                 "confirm", lock_token=old)
    assert await shared.owns_turn_lock(cid, new)
    async with db.SessionLocal() as session:
        assert not (await session.scalar(select(RefundConfirmation.status)
            .where(RefundConfirmation.id == proposal["confirmation"]["id"]))) == "confirmed"
        assert await session.scalar(select(func.count()).select_from(Refund)) == 0
