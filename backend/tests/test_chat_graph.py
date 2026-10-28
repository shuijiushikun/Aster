"""统一 Agent 的真实 MCP 工具链、上下文、事件和用量回归。"""
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import func, select

from app import redis as shared
from app.after_sales.refund_confirmation import RefundConfirmationService
from app.agent import chat_graph, graph
from app.agent.runner import historical_refund_reply, run_agent_turn
from app.agent.streaming import GraphEvents
from app.commerce_mcp import operations
from app.config import get_settings
from app.db import session as db
from app.db.models import AfterSalesTicket, Conversation, KnowledgeDocument, Message, Order, Refund
from app.mcp.tools import build_after_sales_tools
from app.rag.schemas import Evidence, RetrievalResult


def ai(content="", *, call=None, usage=True):
    message = AIMessage(
        content=content,
        tool_calls=[{"name": call[0], "args": call[1], "id": str(uuid4())}] if call else [],
    )
    if usage:
        message.usage_metadata = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    return message


def grounded():
    return ai(json.dumps({"claims": [{"document_id": "KB-HP-01",
        "revision": "1", "chunk_index": 2, "quote": "可先重置耳机", "text": "您可以先尝试重置耳机。"}],
        "missing": []}, ensure_ascii=False))


class Model:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def evidence(model_code="HP-01"):
    return RetrievalResult(
        query="左耳无声怎么办",
        model_code=model_code,
        status="ok",
        sources=[
            Evidence(
                document_id="KB-HP-01",
                title="耳机说明",
                revision="1",
                chunk_index=2,
                category="digital",
                model_code=model_code,
                score=0.9,
                score_kind="rerank",
                excerpt="可先重置耳机，仍异常时提交故障描述，换货需人工审批。",
                source_path="demo.md",
                references=["https://example.com"],
            )
        ],
    )


@pytest.fixture
async def setup_chat(mcp_client, monkeypatch):
    client, _, _ = mcp_client
    monkeypatch.setattr(get_settings(), "agent_v2_enabled", True)
    monkeypatch.setattr(chat_graph, "build_after_sales_tools", lambda: build_after_sales_tools(client))
    monkeypatch.setattr(graph, "get_fallback_model", lambda: None)
    async with db.SessionLocal() as session, session.begin():
        session.add(KnowledgeDocument(id="KB-HP-01", title="耳机说明", category="digital",
            model_code="HP-01", revision="1", status="active", source_path="demo.md", content_hash="test"))

    async def retrieve(query, **scope):
        return evidence(scope.get("model_code"))
    monkeypatch.setattr(operations, "retrieve_knowledge", retrieve)
    return client


def use_model(monkeypatch, *replies):
    model = Model(*replies)
    monkeypatch.setattr(graph, "get_chat_model", lambda: model)
    return model


async def turn(text, customer="CUST-EX-001", cid=None):
    return [event async for event in run_agent_turn(customer, cid, text)]


def final(events):
    return next(event["content"] for event in events if event["kind"] == "message")


async def count_tickets():
    async with db.SessionLocal() as session:
        return await session.scalar(select(func.count()).select_from(AfterSalesTicket))


def exchange_calls():
    return [ai(call=("get_order", {"order_id": "ORD-EX-1001"})),
            ai(call=("search_knowledge", {"query": "左耳无声换货", "order_id": "ORD-EX-1001"})),
            ai(call=("create_exchange_request", {"order_id": "ORD-EX-1001", "reason_type": "defect",
                "description": "左耳无声，希望更换同型号", "requested_change": "同型号"})),
            ai("换货已受理。")]


async def test_exchange_citations_events_and_usage(setup_chat, monkeypatch):
    model = use_model(monkeypatch, *exchange_calls())
    events = await turn("ORD-EX-1001 耳机左耳无声，我要换同型号")
    assert "待工作流处理" in final(events) and "受理已完成" not in final(events)
    ticket = next(e for e in events if e["kind"] == "ticket_update")
    assert ticket["status"] == "OPEN"
    assert any(e["kind"] == "knowledge_sources" and e["sources"][0]["revision"] == "1" for e in events)
    steps = [e for e in events if e["kind"] == "step"]
    assert len([e for e in steps if e["step_type"] == "usage"]) == model.calls == 4
    cid = events[0]["conversation_id"]
    async with db.SessionLocal() as session:
        row = await session.get(AfterSalesTicket, ticket["ticket_id"])
        assert row.evidence_refs[0]["chunk_index"] == 2
        assert (await session.get(Conversation, cid)).tokens_used == 48
        messages = list(await session.scalars(select(Message).where(Message.conversation_id == cid)))
        assert len(messages) == 2 and not messages[-1].content.startswith("{")
    assert await shared.customer_tokens_today("CUST-EX-001") == 48


@pytest.mark.parametrize("name,args,text", [
    ("search_knowledge", {"query": "换货政策"}, "换货政策是什么"),
    ("list_my_orders", {}, "查询订单"),
    ("get_tickets", {}, "查询售后工单"),
])
async def test_read_requests_never_build_ticket(setup_chat, monkeypatch, name, args, text):
    use_model(monkeypatch, ai(call=(name, args)), ai("请参阅返回的资料。"))
    assert final(await turn(text)) and await count_tickets() == 0


@pytest.mark.parametrize("text", ["耳机坏了，我要换货", "ORD-EX-1001 和 ORD-EX-1002 都换货"])
async def test_missing_or_ambiguous_order_blocks_model_guessed_order(setup_chat, monkeypatch, text):
    use_model(monkeypatch, ai(call=("get_order", {"order_id": "ORD-EX-1001"})), ai("请提供唯一订单号。"))
    events = await turn(text)
    results = [e["payload"] for e in events if e.get("step_type") == "tool_result"]
    assert results[0]["result"]["error"]["code"] == "INVALID_ARGUMENT"
    assert await count_tickets() == 0


async def test_unknown_tool_is_safe_and_usage_kept(setup_chat, monkeypatch):
    use_model(monkeypatch, ai(call=("approve_exchange", {})), ai("请在工作台等待审批。"))
    events = await turn("申请换货")
    assert len([e for e in events if e.get("step_type") == "usage"]) == 2
    assert await count_tickets() == 0


@pytest.mark.parametrize("bad", ["order", "missing_evidence", "forged_evidence"])
async def test_model_cannot_change_order_or_forge_evidence(setup_chat, monkeypatch, bad):
    args = {"order_id": "ORD-EX-1002" if bad == "order" else "ORD-EX-1001", "description": "故障换货"}
    if bad == "forged_evidence":
        args["evidence_token"] = "forged"
    use_model(monkeypatch, ai(call=("create_exchange_request", args)), ai("尚未提交申请。"))
    assert "尚未提交" in final(await turn("ORD-EX-1001 我要换货"))
    assert await count_tickets() == 0


async def test_ownership_failure_does_not_retrieve_or_write(setup_chat, monkeypatch):
    async def no_retrieve(*args, **kwargs):
        raise AssertionError("wrong owner must not retrieve")
    monkeypatch.setattr(operations, "retrieve_knowledge", no_retrieve)
    use_model(monkeypatch, ai(call=("search_knowledge", {"query": "故障", "order_id": "ORD-EX-1001"})),
              ai("未能核实订单。"))
    assert "未能核实" in final(await turn("ORD-EX-1001 换货", customer="CUST-EX-002"))
    assert await count_tickets() == 0


async def test_scope_from_server_catalog_and_citations_in_reply(setup_chat, monkeypatch):
    scopes = []
    async def retrieve(query, **scope):
        scopes.append(scope)
        return evidence()
    monkeypatch.setattr(operations, "retrieve_knowledge", retrieve)
    use_model(monkeypatch, ai(call=("search_knowledge", {"query": "左耳无声", "model_code": "HP-01"})),
              ai("可先重置耳机。"), grounded())
    events = await turn("HP-01 左耳无声怎么办")
    assert "耳机说明" in final(events)
    assert [e["node"] for e in events if e.get("step_type") == "usage"].count("answer_grounding") == 1
    assert scopes == [{"model_code": "HP-01", "category": "digital"}]
    assert await count_tickets() == 0


async def test_explicit_scope_cannot_be_omitted_for_generic_search(setup_chat, monkeypatch):
    async def no_retrieve(*args, **kwargs):
        raise AssertionError("must not retrieve without verifying explicit scope")
    monkeypatch.setattr(operations, "retrieve_knowledge", no_retrieve)
    use_model(monkeypatch, ai(call=("search_knowledge", {"query": "排查故障"})), ai("请核实型号。"))
    assert "核实型号" in final(await turn("ORD-EX-2001 耳机怎么排查"))
    assert await count_tickets() == 0


async def test_insufficient_evidence_blocks_application(setup_chat, monkeypatch):
    async def retrieve(query, **kwargs):
        return RetrievalResult(query=query, model_code="HP-01", status="insufficient_evidence", reason="none")
    monkeypatch.setattr(operations, "retrieve_knowledge", retrieve)
    use_model(monkeypatch, *exchange_calls())
    assert "有效资料" in final(await turn("ORD-EX-1001 我要换货"))
    assert await count_tickets() == 0


async def test_unknown_usage_and_provider_failure_keep_prior_usage(setup_chat, monkeypatch):
    use_model(monkeypatch, ai(call=("search_knowledge", {"query": "售后政策"})),
              ai(call=("list_my_orders", {}), usage=False), RuntimeError("provider failed"))
    events = await turn("换货政策是什么")
    usage = [e["payload"] for e in events if e.get("step_type") == "usage"]
    assert [u["status"] for u in usage] == ["reported", "unknown", "unknown"]
    assert usage[1]["input_tokens"] is None and events[-1]["kind"] == "error"
    assert await shared.customer_tokens_today("CUST-EX-001") == 12


async def test_refund_uses_mcp_and_preserves_guard_and_accounting(setup_chat, monkeypatch):
    use_model(monkeypatch, ai(call=("issue_refund", {"order_id": "ORD-1001"})), ai("模拟退款已批准。"))
    events = await turn("ORD-1001 帮我退款", customer="CUST-001")
    assert "确认框" in final(events) and "尚未执行" in final(events)
    assert not any(e.get("step_type") == "output_correction" for e in events)
    assert len([e for e in events if e.get("step_type") == "usage"]) == 1
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Refund).where(
            Refund.order_id == "ORD-1001", Refund.decision == "approved")) == 0
    cid = events[0]["conversation_id"]
    service = RefundConfirmationService()
    proposal = await service.pending("CUST-001", cid)
    assert (await service.decide("CUST-001", cid, proposal["id"], "confirm"))["result"]["decision"] == "approved"


async def test_verified_historical_refund_is_not_reported_as_unconfirmed(setup_chat, monkeypatch):
    use_model(monkeypatch, ai(call=("issue_refund", {"order_id": "ORD-1001"})), ai("模拟退款已批准。"))
    first = await turn("ORD-1001 帮我退款", customer="CUST-001")
    cid = first[0]["conversation_id"]
    service = RefundConfirmationService()
    proposal = await service.pending("CUST-001", cid)
    await service.decide("CUST-001", cid, proposal["id"], "confirm")
    use_model(monkeypatch, ai("这笔订单已经成功退款，已经记录过一次。"))
    second = await turn("这笔订单再退款一次", customer="CUST-001", cid=cid)
    assert "未经系统确认" not in final(second)
    assert not any(e.get("step_type") == "output_correction" for e in second)
    assert "此前" in final(second) and "ORD-1001" in final(second)
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Refund).where(
            Refund.order_id == "ORD-1001", Refund.decision == "approved")) == 1


async def test_other_owned_refund_does_not_validate_a_foreign_order_claim(setup_chat, monkeypatch):
    use_model(monkeypatch, ai(call=("issue_refund", {"order_id": "ORD-1001"})), ai("模拟退款已批准。"))
    first = await turn("ORD-1001 帮我退款", customer="CUST-001")
    use_model(monkeypatch, ai("ORD-EX-2001 已退款成功。"))
    events = await turn("查询 ORD-EX-2001 的退款状态", customer="CUST-001", cid=first[0]["conversation_id"])
    assert any(e.get("step_type") == "output_correction" for e in events)


async def test_refunded_flag_without_approved_ledger_cannot_validate_success(setup_chat):
    async with db.SessionLocal() as session, session.begin():
        (await session.get(Order, "ORD-1001")).refunded = True
    assert await historical_refund_reply("CUST-001", [HumanMessage("查询 ORD-1001")], "退款成功", {"ORD-1001"}) is None


async def test_failed_historical_database_check_keeps_original_output_guard(setup_chat, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    def unavailable():
        raise SQLAlchemyError("test outage")

    with monkeypatch.context() as scoped:
        scoped.setattr(db, "SessionLocal", unavailable)
        assert await historical_refund_reply("CUST-001", [HumanMessage("查询 ORD-1001")], "退款成功", set()) is None


async def test_escalated_refund_does_not_send_customer_to_exchange_tickets(setup_chat, monkeypatch):
    from app.db.models import Order
    async with db.SessionLocal() as session, session.begin():
        (await session.get(Order, "ORD-1001")).amount = 999
    use_model(monkeypatch, ai(call=("escalate_to_human", {"order_id": "ORD-1001", "reason": "金额超限"})),
              ai("已转人工，审核结果可在我的工单查看。"))
    events = await turn("ORD-1001 请帮我退款", customer="CUST-001")
    assert "人工" in final(events) and "尚未执行" in final(events)
    assert "我的工单" not in final(events)
    service = RefundConfirmationService()
    cid = events[0]["conversation_id"]
    proposal = await service.pending("CUST-001", cid)
    assert (await service.decide("CUST-001", cid, proposal["id"], "confirm"))["result"]["decision"] == "escalated"
    async with db.SessionLocal() as session:
        saved = await session.scalar(select(Message).where(Message.conversation_id == cid)
                                     .order_by(Message.id.desc()).limit(1))
        assert "尚未批准" in saved.content and "联系人工客服" in saved.content


@pytest.mark.parametrize("text", ["ORD-1001 能退款吗", "ORD-1001 不要退款，只查询订单"])
async def test_consultation_rejects_model_write(setup_chat, monkeypatch, text):
    use_model(monkeypatch, ai(call=("issue_refund", {"order_id": "ORD-1001"})), ai("尚未办理退款。"))
    await turn(text, customer="CUST-001")
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Refund)) == 0


async def test_single_agent_tool_budget_stops_loop(setup_chat, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_max_tool_calls", 3)
    model = use_model(monkeypatch, *(ai(call=("list_my_orders", {})) for _ in range(4)))
    events = await turn("查询订单")
    assert model.calls == 3
    assert len([e for e in events if e.get("step_type") == "tool_result"]) == 3
    assert final(events)


async def test_query_reply_cannot_claim_exchange_success(setup_chat, monkeypatch):
    use_model(monkeypatch, ai("换货已受理。"))
    assert "尚未取得" in final(await turn("查一下换货政策"))
    assert chat_graph.claims_exchange_success("Your exchange has been approved.")
    assert not chat_graph.claims_exchange_success("换货尚未受理，审批后才能处理。")
    text = chat_graph.ticket_reply({"id": "TKT-1", "order_id": "ORD-1", "status": "COMPLETED"})
    assert "凭证尚待核实" in text and "受理已完成" not in text


async def test_short_order_followup_reaches_verified_order(setup_chat, monkeypatch):
    use_model(monkeypatch, ai(call=("get_order", {"order_id": "ORD-1001"})), ai("已查到这笔订单。"))
    events = await turn("1001的订单情况", customer="CUST-001")
    results = [e["payload"]["result"] for e in events if e.get("step_type") == "tool_result"]
    assert results[0]["ok"] and results[0]["data"]["order_id"] == "ORD-1001"


async def test_general_product_knowledge_uses_catalog_without_order_selection(setup_chat, monkeypatch):
    use_model(monkeypatch, ai(call=("search_knowledge", {"query": "耳机单侧无声怎么排查", "model_code": "HP-01"})),
              ai("可以先检查连接，再尝试排查。"), grounded())
    events = await turn("随便看看耳机相关知识")
    assert "耳机说明" in final(events)
    assert any(e["kind"] == "knowledge_sources" and e["sources"] for e in events)


async def test_knowledge_outage_is_explained_separately_from_missing_specification(setup_chat, monkeypatch):
    async def unavailable(query, **kwargs):
        return RetrievalResult(query=query, model_code="HP-01", status="unavailable", reason="service_unavailable")
    monkeypatch.setattr(operations, "retrieve_knowledge", unavailable)
    use_model(monkeypatch, ai(call=("search_knowledge", {"query": "续航", "model_code": "HP-01"})), ai("可能没有资料。"))
    text = final(await turn("HP-01 续航多久"))
    assert "没有连通" in text and "不用反复输入" in text


async def test_missing_evidence_names_requested_fact_without_trusting_draft(setup_chat, monkeypatch):
    async def missing(query, **kwargs):
        return RetrievalResult(query=query, model_code="TSHIRT-01", status="insufficient_evidence")
    monkeypatch.setattr(operations, "retrieve_knowledge", missing)
    answer = "可以放心使用80度烘干。"
    use_model(monkeypatch, ai(call=("search_knowledge", {"query": "烘干", "model_code": "TSHIRT-01"})), ai(answer))
    text = final(await turn("TSHIRT-01 能烘干吗"))
    assert "烘干" in text and "尚未找到直接依据" in text
    assert "80度" not in text and "耳机" not in text


def test_ticket_reply_explains_next_step_and_preserves_acceptance_reference():
    pending = chat_graph.ticket_reply({"id": "TKT-1", "order_id": "ORD-1", "status": "WAITING_INFO"})
    assert "补充具体问题" in pending and "模拟换货受理已完成" not in pending
    completed = chat_graph.ticket_reply({"id": "TKT-1", "order_id": "ORD-1", "status": "COMPLETED", "reference_code": "EXC-123"})
    assert "EXC-123" in completed and "不安排实际发货" in completed


@pytest.mark.parametrize("text", [
    "等待补充和审批不会让已按时提交的申请过期。",
    "只有到这一步，才能告知您换货已受理。",
    "换货在**受理成功之前**（包括待审批期间），仍可按规则申请退款。",
])
def test_exchange_process_explanations_are_not_personal_success_claims(text):
    assert not chat_graph.claims_exchange_success(text)


@pytest.mark.parametrize("text", [
    "换货已受理，之后请查看记录。",
    "您无需再次提交，已为您完成换货。",
    "只有审批后才能受理。不过现在工单已完成。",
])
def test_exchange_success_claim_is_still_checked_beside_process_explanation(text):
    assert chat_graph.claims_exchange_success(text)


def test_events_deduplicated_and_intermediate_messages_never_final():
    adapter = GraphEvents(v2=True)
    message = ai("准备查询订单")
    message.id = "one"
    assert len(list(adapter.adapt(((), {"agent": {"messages": [message]}})))) == 1
    assert list(adapter.adapt(((), {"agent": {"messages": [message]}}))) == []
    tool_msg = ToolMessage("ok", tool_call_id="call", name="get_order", id="two")
    assert next(adapter.adapt(((), {"tools": {"messages": [tool_msg]}})))[0] == "message"
