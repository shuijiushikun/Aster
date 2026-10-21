"""供压力测试和端到端检查使用、支持工具调用的脚本模拟聊天模型。

它按预设流程读取客户的最新消息，选择工具调用顺序（查询订单 → 检查退款资格
→ 办理退款或转人工），然后生成最终回复。每次调用按以 `latency_ms` 为中位数的
对数正态分布模拟等待时间，并以 `error_rate` 的概率失败，使压力测试能够覆盖
长时间保持的 SSE 流和错误处理路径，而不消耗模型词元。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.llm import FAKE_MODEL_NAME
from app.localization import ORDER_STATUS, product_name

# 中文与订单号相邻时不能使用 Unicode 的 \b，否则“订单ORD-1001退款”无法匹配。
_ORDER_RE = re.compile(r"(?<![A-Z0-9-])[A-Z]{2,4}-\d{3,}(?:-[A-Z0-9]+)?(?![A-Z0-9-])", re.I)
# 退款申请与否定表达共用动作词，避免新增说法后“不要退掉”等被误触发。
_ZH_REFUND_ACTION = r"(?:退款|退货|退钱|退回货款|退掉|退一次款)"
_REFUND_RE = re.compile(r"\b(refund|return|money back)\b|" + _ZH_REFUND_ACTION, re.I)
_ORDERS_RE = re.compile(r"\b(orders?|purchases?|bought)\b|订单|买过|购买记录", re.I)
_NO_REFUND_RE = re.compile(
    r"(?:不要|不想|无需|不用|不需要)\s*(?:再|再次)?"
    r"(?:(?:给|为)(?:我|该订单|这笔订单|订单\s*[A-Z0-9-]+))?"
    r"\s*(?:再|再次)?(?:办理|申请)?" + _ZH_REFUND_ACTION, re.I,
)


class FakeLLMError(RuntimeError):
    pass


def _call(name: str, args: dict) -> dict:
    return {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "tool_call"}


class ScriptedFakeChatModel:
    def __init__(self, latency_ms: int = 1500, error_rate: float = 0.0):
        self.latency_ms = latency_ms
        self.error_rate = error_rate

    def bind_tools(self, _tools):
        return self

    async def _sleep(self) -> None:
        if self.latency_ms <= 0:
            return
        # 对数正态分布：中位数为 latency_ms，sigma 为 0.4，p95 约为中位数的 1.9 倍。
        seconds = random.lognormvariate(math.log(self.latency_ms / 1000), 0.4)
        await asyncio.sleep(min(seconds, 30.0))

    async def ainvoke(self, messages, *args, **kwargs) -> AIMessage:
        await self._sleep()
        if self.error_rate and random.random() < self.error_rate:
            raise FakeLLMError("simulated provider error")
        msg = self._decide(messages)
        chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
        msg.usage_metadata = {
            "input_tokens": chars // 4,
            "output_tokens": 40,
            "total_tokens": chars // 4 + 40,
        }
        msg.response_metadata = {"model": FAKE_MODEL_NAME}
        return msg

    def _decide(self, messages) -> AIMessage:
        # 仅处理本轮内容，即最新一条用户消息及其后的所有消息。
        last_human = max(
            (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1
        )
        text = str(messages[last_human].content) if last_human >= 0 else ""
        turn = messages[last_human + 1 :]
        results: dict[str, dict] = {}
        for m in turn:
            if isinstance(m, ToolMessage):
                try:
                    results[m.name] = json.loads(m.content)
                except (TypeError, json.JSONDecodeError):
                    results[m.name] = {}

        order_match = _ORDER_RE.search(text)
        wants_refund = bool(_REFUND_RE.search(text)) and not _NO_REFUND_RE.search(text)

        if "issue_refund" in results:
            r = results["issue_refund"]
            if r.get("decision") == "approved":
                return AIMessage(
                    content=f"订单 {r.get('order_id')} 的退款已批准，金额为 {r.get('amount', 0):.2f} 美元。"
                )
            return AIMessage(
                content="抱歉，暂时无法批准这笔退款。" + " ".join(r.get("reasons", []))
            )
        if "escalate_to_human" in results:
            r = results["escalate_to_human"]
            if r.get("error"):
                return AIMessage(content="这笔订单不符合转人工条件。" + " ".join(r.get("reasons", [])))
            if r.get("decision") == "approved":
                return AIMessage(content="该订单此前已退款，不能重复办理。")
            return AIMessage(
                content="该申请已转交人工审核，请等待审核结果。"
                if not r.get("already_escalated") else "该申请此前已转交人工审核，无需重复提交。"
            )
        if "check_refund_eligibility" in results:
            r = results["check_refund_eligibility"]
            oid = r.get("order_id") or (order_match.group(0).upper() if order_match else "")
            if r.get("error"):
                return AIMessage(content="未找到当前账户下的该笔订单，请核对订单号。")
            if r.get("decision") == "approved":
                return AIMessage(content="", tool_calls=[_call("issue_refund", {"order_id": oid})])
            if r.get("decision") == "escalated":
                return AIMessage(
                    content="",
                    tool_calls=[
                        _call(
                            "escalate_to_human",
                            {"order_id": oid, "reason": "退款金额超过自动批准上限，需要人工审核。"},
                        )
                    ],
                )
            return AIMessage(
                content="抱歉，这笔订单不符合退款条件："
                + " ".join(r.get("reasons", []))
            )
        if "list_orders" in results:
            orders = results["list_orders"].get("orders", [])
            if wants_refund and orders:
                return AIMessage(
                    content="",
                    tool_calls=[
                        _call("check_refund_eligibility", {"order_id": orders[0]["order_id"]})
                    ],
                )
            listing = "\n".join(
                f"- {o['order_id']}：{product_name(o['product_name'])}，{o.get('amount', 0):.2f} 美元"
                for o in orders
            )
            return AIMessage(content=f"您的订单如下：\n{listing}" if listing else "当前账户暂无订单。")

        if "get_order" in results:
            r = results["get_order"]
            if r.get("error"):
                return AIMessage(content="未找到当前账户下的该笔订单，请核对订单号。")
            return AIMessage(content=(
                f"订单 {r.get('order_id')}：{product_name(r.get('product_name', '商品'))}，"
                f"金额 {r.get('amount', 0):.2f} 美元，"
                f"状态：{ORDER_STATUS.get(r.get('status'), '待确认')}。"
                + ("该订单此前已退款。" if r.get("already_refunded") else "")
            ))

        # 本轮对话的第一步。
        if order_match:
            oid = order_match.group(0).upper()
            if wants_refund:
                return AIMessage(
                    content="", tool_calls=[_call("check_refund_eligibility", {"order_id": oid})]
                )
            return AIMessage(content="", tool_calls=[_call("get_order", {"order_id": oid})])
        if wants_refund or _ORDERS_RE.search(text):
            return AIMessage(content="", tool_calls=[_call("list_orders", {})])
        return AIMessage(
            content="我只能协助处理 ACME 商店订单的退款问题。您想查询哪笔订单？"
        )
