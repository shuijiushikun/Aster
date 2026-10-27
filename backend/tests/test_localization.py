"""中文体验回归：既检查语言，也核对退款工具和账本的真实结果。"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.llm_fake import ScriptedFakeChatModel
from app.agent.runner import claims_approval, create_conversation, run_agent_turn
from tests.conftest import auth
from tests.test_resilience import FakeModel


@pytest.mark.parametrize("text", ["我要退款", "退货 ORD-1001", "请退钱给我", "refund ORD-1001", "我不想要了，请给订单 ORD-1001 退款"])
def test_scripted_refund_intent(text):
    reply = ScriptedFakeChatModel()._decide([HumanMessage(content=text)])
    assert reply.tool_calls[0]["name"] == (
        "check_refund_eligibility" if "ORD-" in text else "list_orders"
    )


@pytest.mark.parametrize("text", ["查询我的订单", "我买过什么", "我有哪些购买记录"])
def test_scripted_order_intent(text):
    reply = ScriptedFakeChatModel()._decide([HumanMessage(content=text)])
    assert reply.tool_calls[0]["name"] == "list_orders"


def test_order_id_next_to_chinese():
    reply = ScriptedFakeChatModel()._decide([HumanMessage(content="请退款订单ORD-1001谢谢")])
    assert reply.tool_calls[0]["args"]["order_id"] == "ORD-1001"


@pytest.mark.parametrize("text", ["我不想退款，只查询订单 ORD-1001", "不要给订单ORD-1001退款"])
def test_chinese_explicit_no_refund(text):
    reply = ScriptedFakeChatModel()._decide([HumanMessage(content=text)])
    assert reply.tool_calls[0]["name"] == "get_order"


@pytest.mark.parametrize("text", [
    "退款已批准。", "已为您成功办理退款。", "退款处理成功。", "已为您退款。",
    "虽然无法跳过规则，但退款已批准。", "退款尚未批准。不过现在已为您退款。",
    "您的退款申请已获批准。", "我已经批准了您的退款。", "退款已成功处理。",
    # 订单主语不能自动视为历史说明；这些肯定句仍须核对本轮退款工具的结果。
    "订单 ORD-1001 已为您办理退款。", "该订单已为您成功办理退款。",
    "订单ORD-1001已经处理退款。", "订单 ORD-1001 已退款。",
    "订单 ORD-1001 退款已成功处理。",
    "订单 ORD-1001 已为您办理退款，请勿重复申请。",
    "订单 ORD-1001 此前已经退款。但现在已为您办理退款。",
    "已为您退款。已退款订单不能再次退款。",
    "退款已批准。退款成功会终止活跃的换货申请。",
])
def test_chinese_approval_claim(text):
    assert claims_approval(text)


@pytest.mark.parametrize("text", [
    "退款尚未批准。", "无法为您办理退款。", "审核通过后才能退款。",
    "这笔订单此前已退款，不能重复退款。", "退款未获批准。", "我不能声称退款已批准。",
    "无法确认已为您退款。", "如果退款已批准，系统会显示记录。",
    "退款已批准后才会到账，目前仍在审核。", "该订单已经退款，无法再次办理。",
    "订单 ORD-1004 已经退款，不能重复办理。", "目前无法确认您的退款已批准。",
    "每笔订单只能成功退款一次：已退款订单不能再次退款。",
    "退款成功会终止活跃的换货申请。",
    "订单 ORD-1001 此前已为您办理退款，本次没有重复办理。",
    "订单 ORD-1001 已退款，不能重复办理。",
    "如果订单 ORD-1001 已为您办理退款，会显示退款记录。",
])
def test_chinese_denial_history_and_conditional_are_not_approval(text):
    assert not claims_approval(text)


@pytest.mark.parametrize(("customer", "order", "decision"), [
    ("CUST-001", "ORD-1001", "approved"),
    ("CUST-002", "ORD-1002", "denied"),
    ("CUST-003", "ORD-1003", "escalated"),
])
async def test_chinese_refund_flow(engine, monkeypatch, customer, order, decision):
    from app.agent import graph

    monkeypatch.setattr(graph, "get_chat_model", lambda: ScriptedFakeChatModel(latency_ms=0))
    cid = await create_conversation(customer)
    events = [e async for e in run_agent_turn(customer, cid, f"请给订单{order}退款")]
    results = [e["payload"]["result"] for e in events if e.get("step_type") in ("decision", "policy_eval")]
    assert results[-1]["decision"] == decision
    content = next(e["content"] for e in events if e["kind"] == "message")
    assert any("\u4e00" <= c <= "\u9fff" for c in content)
    assert not any(e.get("step_type") == "output_correction" for e in events)


@pytest.mark.parametrize("reply", [
    "已为您退款。",
    "订单 ORD-1001 已为您办理退款。",
    "订单 ORD-1001 退款已成功处理。",
])
async def test_unbacked_chinese_approval_is_corrected(engine, monkeypatch, reply):
    from app.agent import graph

    monkeypatch.setattr(graph, "get_chat_model", lambda: FakeModel([AIMessage(content=reply)]))
    cid = await create_conversation("CUST-001")
    events = [e async for e in run_agent_turn("CUST-001", cid, "我要退款")]
    assert any(e.get("step_type") == "output_correction" for e in events)
    content = next(e["content"] for e in events if e["kind"] == "message")
    assert content.startswith("_系统提示")


async def test_chinese_http_validation(client):
    response = await client.post("/api/chat", json={"message": " "}, headers=auth("CUST-001"))
    assert response.status_code == 400
    assert "不能为空" in response.json()["detail"]


async def test_scripted_unknown_order_does_not_expose_raw_error():
    from langchain_core.messages import ToolMessage

    reply = ScriptedFakeChatModel()._decide([
        HumanMessage(content="退货 ORD-9999"),
        ToolMessage(content=json.dumps({"error": "order_not_found"}), name="check_refund_eligibility", tool_call_id="1"),
    ])
    assert "order_not_found" not in reply.content
    assert "订单" in reply.content
