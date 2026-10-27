"""每轮分类在聊天与真实 MCP 协议之间复用，仍受业务权限约束。"""

import httpx
import jwt
import pytest
from sqlalchemy import func, select

from app.after_sales.intent import TurnIntent, classify_intent, message_digest
from app.agent import chat_graph, runner
from app.config import get_settings
from app.db import session as db
from app.db.models import Message, Refund
from app.mcp.auth import mint_service_token
from tests.test_chat_graph import ai, final, turn, use_model
from tests.test_chat_graph import setup_chat as setup_chat
from tests.test_commerce_mcp import ALICE, BOB, turn_context
from tests.test_intent_classifier import factory, response


def classified(text, intent="refund"):
    return TurnIntent(intent=intent, source="model", message_sha256=message_digest(text)).model_dump()


@pytest.mark.parametrize("text,intent,approved", [
    ("ORD-EX-1001 可以帮我办理退款吗？", "refund", True),
    ("ORD-EX-1001 我要退款", "query", False),
    ("ORD-EX-1001 我要退款", "clarify", False),
    ("ORD-EX-1001 我要退款", "exchange", False),
])
@pytest.mark.usefixtures("setup_chat")
async def test_chat_and_mcp_use_one_decision_and_account_once(monkeypatch, text, intent, approved):
    calls = []
    exposed = []
    monkeypatch.setattr(get_settings(), "intent_classifier_enabled", True)

    async def classify(messages, config):
        calls.append(messages)
        return await classify_intent(messages, config, http_client_factory=factory(lambda _: response(intent)))

    monkeypatch.setattr(chat_graph, "classify_intent", classify)
    # 分类模型是独立辅助调用，不应被计作 DeepSeek 故障切换。
    def unexpected_fallback(*args):
        raise AssertionError("intent model is not the conversation fallback")
    monkeypatch.setattr(runner.LLM_FALLBACKS, "labels", unexpected_fallback)
    model = use_model(monkeypatch, ai(call=("issue_refund", {"order_id": "ORD-EX-1001"})),
                      ai("已记录模拟退款。" if approved else "请补充确认。"))

    def bind(tools):
        exposed.append({tool.name for tool in tools})
        return model
    model.bind_tools = bind
    events = await turn(text)
    assert final(events)
    assert len(calls) == 1 and len(exposed) == 2
    assert all(("issue_refund" in names) is (intent == "refund") for names in exposed)
    assert all(("create_exchange_request" in names) is (intent == "exchange") for names in exposed)
    usage = [e for e in events if e.get("step_type") == "usage"]
    assert len(usage) == 3
    assert sum(e["payload"]["input_tokens"] + e["payload"]["output_tokens"] for e in usage) == 132
    assert len({e["payload"]["call_id"] for e in usage}) == 3
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Refund)) == int(approved)


@pytest.mark.parametrize("case", ["wrong_hash", "query", "other_customer", "wrong_order", "altered_message"])
async def test_signed_classification_does_not_override_persisted_scope(mcp_client, case):
    client, _, _ = mcp_client
    text = "ORD-EX-1001 可以帮我办理退款吗？"
    context = await turn_context(text)
    context["intent"] = classified(text, "query" if case == "query" else "refund")
    if case == "wrong_hash":
        context["intent"]["message_sha256"] = message_digest("another message")
    if case == "altered_message":
        async with db.SessionLocal() as session, session.begin():
            message = await session.get(Message, context["message_id"])
            message.content = "不要退款"
    result = await client.call(BOB if case == "other_customer" else ALICE, "issue_refund",
        {"order_id": "ORD-EX-1002" if case == "wrong_order" else "ORD-EX-1001"}, turn_context=context)
    assert not result.ok and result.error.code in {"ACTION_NOT_REQUESTED", "INVALID_ARGUMENT"}
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Refund)) == 0


async def test_old_mcp_call_without_decision_keeps_conservative_gate(mcp_client):
    client, _, _ = mcp_client
    context = await turn_context("ORD-EX-1001 可以帮我办理退款吗？")
    result = await client.call(ALICE, "issue_refund", {"order_id": "ORD-EX-1001"}, turn_context=context)
    assert result.error.code == "ACTION_NOT_REQUESTED"


async def test_classifier_context_is_hidden_from_model_tool_parameters(mcp_client):
    client, _, _ = mcp_client
    for tool in await client.list_tools(ALICE):
        assert not {"intent", "turn_intent", "message_sha256", "message_id", "conversation_id"} & tool.inputSchema["properties"].keys()


async def test_server_rejects_signed_but_invalid_classification(mcp_client, mcp_settings):
    _, app, _ = mcp_client
    token = mint_service_token(ALICE, mcp_settings, **await turn_context("ORD-EX-1001 我要退款"))
    claims = jwt.decode(token, mcp_settings.commerce_mcp_service_secret, algorithms=["HS256"],
                        audience="commerce-mcp")
    claims["intent"] = {"intent": "approve", "source": "model", "message_sha256": "x"}
    token = jwt.encode(claims, mcp_settings.commerce_mcp_service_secret, algorithm="HS256")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        result = await http.post("/mcp", json={}, headers={"Authorization": "Bearer " + token})
    assert result.status_code == 401 and token not in result.text
