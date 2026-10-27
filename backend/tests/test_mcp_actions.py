"""写工具证据、重放和失败语义；通过真实 MCP SDK/ASGI 执行。"""

import json

import httpx
import pytest
from sqlalchemy import func, select

from app.after_sales.intent import requested_actions
from app.after_sales.schemas import RequestInput
from app.after_sales.service import TicketService
from app.agent.tools import ToolContext, tool_config
from app.commerce_mcp import operations
from app.db import session as db
from app.db.models import AfterSalesTicket, KnowledgeDocument, Message
from app.mcp.client import CommerceClient
from app.mcp.tools import build_after_sales_tools
from app.rag.schemas import Evidence, RetrievalResult
from tests.test_commerce_mcp import ALICE, turn_context


@pytest.fixture
async def knowledge(mcp_client, monkeypatch):
    scopes = []
    async with db.SessionLocal() as session, session.begin():
        session.add(KnowledgeDocument(id="KB-TEST", title="型号资料", category="digital",
            model_code="HP-01", revision="1", status="active", source_path="test.md", content_hash="test"))

    async def retrieve(query, **scope):
        scopes.append(scope)
        return RetrievalResult(query=query, model_code=scope["model_code"], status="ok", sources=[
            Evidence(document_id="KB-TEST", title="型号资料", revision="1", chunk_index=0,
                     category="digital", model_code="HP-01", score=0.9, score_kind="cosine",
                     excerpt="故障需人工审批", source_path="test.md", references=["https://example.test"])
        ])
    monkeypatch.setattr(operations, "retrieve_knowledge", retrieve)
    return scopes


async def proof(client, context):
    result = await client.call(ALICE, "search_knowledge",
        {"query": "故障换货", "order_id": "ORD-EX-1001", "model_code": "TSHIRT-01"}, turn_context=context)
    assert result.ok
    return result.data.evidence_token


async def test_order_scope_overrides_model_and_exchange_replay_survives_cancellation(mcp_client, knowledge):
    client, _, _ = mcp_client
    context = await turn_context("ORD-EX-1001 左耳无声，我要换货")
    token = await proof(client, context)
    assert knowledge == [{"model_code": "HP-01", "category": "digital"}]
    args = {"order_id": "ORD-EX-1001", "description": "左耳无声", "reason_type": "defect", "evidence_token": token}
    first = await client.call(ALICE, "create_exchange_request", args, turn_context=context)
    assert first.ok and first.data.status == "OPEN"
    await TicketService().validate_workflow(first.data.id)
    await TicketService().cancel(ALICE, first.data.id, RequestInput(request_id="cancel"))
    # 已提交请求先查持久化结果，即使短时检索凭据已失效也不重新建单。
    replay = await client.call(ALICE, "create_exchange_request", args | {"evidence_token": "expired"}, turn_context=context)
    assert replay.ok and replay.data.id == first.data.id and replay.data.status == "CANCELLED"
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(AfterSalesTicket)) == 1
    changed = await client.call(ALICE, "create_exchange_request", args | {"description": "不同诉求"}, turn_context=context)
    assert changed.error.code == "REQUEST_CONFLICT"


@pytest.mark.parametrize("bad", ["missing", "tampered", "other_turn", "inactive", "revision"])
async def test_exchange_rejects_invalid_evidence(mcp_client, knowledge, bad):
    client, _, _ = mcp_client
    context = await turn_context("ORD-EX-1001 左耳无声，我要换货")
    token = await proof(client, context)
    if bad == "missing":
        token = None
    elif bad == "tampered":
        token = "invalid." + token
    elif bad == "other_turn":
        context = await turn_context("ORD-EX-1001 左耳无声，我要换货")
    else:
        async with db.SessionLocal() as session, session.begin():
            doc = await session.get(KnowledgeDocument, "KB-TEST")
            if bad == "inactive":
                doc.status = "inactive"
            else:
                doc.revision = "2"
    result = await client.call(ALICE, "create_exchange_request",
        {"order_id": "ORD-EX-1001", "description": "故障", "evidence_token": token}, turn_context=context)
    assert result.error.code == "EVIDENCE_REQUIRED"
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(AfterSalesTicket)) == 0


async def test_write_transport_failure_reports_unknown_outcome(mcp_settings):
    async def fail(request):
        raise httpx.ReadTimeout("response lost", request=request)
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(fail), **kwargs)
    client = CommerceClient(mcp_settings, http_client_factory=factory)
    result = await client.call(ALICE, "issue_refund", {"order_id": "ORD-EX-1001"})
    assert result.error.code == "OUTCOME_UNKNOWN"


async def test_adapter_keeps_retrieval_proof_out_of_model_messages_and_arguments(mcp_client, knowledge):
    client, _, _ = mcp_client
    context = await turn_context("ORD-EX-1001 我要换货")
    ctx = ToolContext(context["conversation_id"], ALICE.sub, message_id=context["message_id"])
    tools = {t.name: t for t in build_after_sales_tools(client)}
    content = await tools["search_knowledge"].ainvoke(
        {"query": "故障", "order_id": "ORD-EX-1001"}, config=tool_config(ctx))
    assert ctx.evidence_tokens["ORD-EX-1001"] not in content
    assert json.loads(content)["data"]["evidence_token"] is None
    assert "evidence_token" not in tools["create_exchange_request"].tool_call_schema.model_json_schema()["properties"]


async def test_overview_does_not_grant_exchange_evidence(mcp_client, knowledge):
    client, _, _ = mcp_client
    context = await turn_context("ORD-EX-1001 我要换货")
    overview = await client.call(ALICE, "search_knowledge",
        {"query": "耳机相关知识", "order_id": "ORD-EX-1001", "overview": True}, turn_context=context)
    assert overview.ok and overview.data.sources
    assert overview.data.evidence_token is None
    assert knowledge == [{"model_code": "HP-01", "category": "digital", "overview": True}]
    exchange = await client.call(ALICE, "create_exchange_request",
        {"order_id": "ORD-EX-1001", "description": "故障", "evidence_token": None}, turn_context=context)
    assert exchange.error.code == "EVIDENCE_REQUIRED"
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(AfterSalesTicket)) == 0


async def test_mcp_rechecks_previous_short_order_reference_for_explicit_followup(mcp_client):
    client, _, _ = mcp_client
    context = await turn_context("1001的订单")
    async with db.SessionLocal() as session, session.begin():
        current = Message(conversation_id=context["conversation_id"], role="user", content="帮我退款")
        session.add(current)
        await session.flush()
        context["message_id"] = current.id
    result = await client.call(ALICE, "issue_refund", {"order_id": "ORD-EX-1001"}, turn_context=context)
    assert result.ok and result.data.decision == "approved"


@pytest.mark.parametrize("text,expected", [
    ("ORD-1001 我要退款", {"refund"}), ("ORD-1001 帮我退货", {"refund"}),
    ("ORD-1001 我要换同型号", {"exchange"}), ("please refund ORD-1001", {"refund"}),
    ("ORD-1001 能退款吗", set()), ("ORD-1001 我退款了吗", set()),
    ("取消退款 ORD-1001", set()), ("ORD-1001 不退款", set()),
    ("Can I refund ORD-1001?", set()), ("cancel refund ORD-1001", set()),
    ("我想了解退款条件", set()), ("退款或换货 ORD-1001", set()),
])
def test_write_request_gate(text, expected):
    assert requested_actions(text) == expected
