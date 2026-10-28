"""脚本模型的演示文案回归；可用 unittest 单独运行，无数据库或模型 API。"""

import json
import unittest

from langchain_core.messages import HumanMessage, ToolMessage

from app.agent.llm_fake import ScriptedFakeChatModel


class ScriptedRefundIntentTests(unittest.TestCase):
    def setUp(self):
        self.model = ScriptedFakeChatModel(latency_ms=0, error_rate=0)

    def test_demo_buttons_start_refund_eligibility_check(self):
        # 与 frontend/src/lib/scenarios.ts 的两个问题按钮原文一致。
        for text, order_id in [
            ("我想退掉订单 ORD-1003 的55 英寸 4K OLED 电视。", "ORD-1003"),
            ("请给订单 ORD-1004 再退一次款。", "ORD-1004"),
            ("请帮我退款 ORD-DEMO-1001", "ORD-DEMO-1001"),
            ("申请退款 ORD-EX-1001", "ORD-EX-1001"),
        ]:
            with self.subTest(text=text):
                reply = self.model._decide([HumanMessage(content=text)])
                self.assertEqual(reply.tool_calls[0]["name"], "check_refund_eligibility")
                self.assertEqual(reply.tool_calls[0]["args"], {"order_id": order_id})

    def test_high_value_request_follows_eligibility_result_to_manual_review(self):
        messages = [HumanMessage(content="我想退掉订单 ORD-1003 的55 英寸 4K OLED 电视。")]
        check = self.model._decide(messages)
        self.assertEqual(check.tool_calls[0]["name"], "check_refund_eligibility")
        messages.extend([
            check,
            ToolMessage(
                content=json.dumps({"order_id": "ORD-1003", "decision": "escalated"}),
                name="check_refund_eligibility", tool_call_id=check.tool_calls[0]["id"],
            ),
        ])
        escalate = self.model._decide(messages)
        self.assertEqual(escalate.tool_calls[0]["name"], "escalate_to_human")
        self.assertEqual(escalate.tool_calls[0]["args"]["order_id"], "ORD-1003")
        messages.extend([
            escalate,
            ToolMessage(
                content=json.dumps({"order_id": "ORD-1003", "decision": "escalated"}),
                name="escalate_to_human", tool_call_id=escalate.tool_calls[0]["id"],
            ),
        ])
        final = self.model._decide(messages)
        self.assertFalse(final.tool_calls)
        self.assertIn("人工审核", final.content)

    def test_repeat_request_stops_when_eligibility_is_denied(self):
        messages = [HumanMessage(content="请给订单 ORD-1004 再退一次款。")]
        check = self.model._decide(messages)
        self.assertEqual(check.tool_calls[0]["name"], "check_refund_eligibility")
        messages.extend([
            check,
            ToolMessage(
                content=json.dumps({
                    "order_id": "ORD-1004", "decision": "denied",
                    "reasons": ["该订单已经退款，不能重复办理。"],
                }, ensure_ascii=False),
                name="check_refund_eligibility", tool_call_id=check.tool_calls[0]["id"],
            ),
        ])
        final = self.model._decide(messages)
        self.assertFalse(final.tool_calls)
        self.assertIn("不能重复办理", final.content)

    def test_existing_refund_and_query_requests_keep_their_routing(self):
        for text, expected in [
            ("我要退款", "list_orders"),
            ("请退钱给我", "list_orders"),
            ("我想申请最近一笔订单的退款。", "list_orders"),
            ("退货 ORD-1001", "check_refund_eligibility"),
            ("退回货款 ORD-1001", "check_refund_eligibility"),
            ("refund ORD-1001", "check_refund_eligibility"),
            ("return ORD-1001", "check_refund_eligibility"),
            ("money back ORD-1001", "check_refund_eligibility"),
            ("请退款订单ORD-1001谢谢", "check_refund_eligibility"),
            ("我不想要了，请给订单 ORD-1001 退款。", "check_refund_eligibility"),
            ("订单 ORD-1001 现在还能退款吗？", "check_refund_eligibility"),
            ("我有哪些订单？", "list_orders"),
            ("查询订单 ORD-1001", "get_order"),
        ]:
            with self.subTest(text=text):
                reply = self.model._decide([HumanMessage(content=text)])
                self.assertEqual(reply.tool_calls[0]["name"], expected)

    def test_explicit_no_refund_requests_only_query_the_order(self):
        for text in [
            "我不想退款，只查询订单 ORD-1001",
            "不要给订单ORD-1001退款",
            "不要退掉订单 ORD-1001",
            "不想退掉订单 ORD-1001",
            "不用再退一次款，只查询订单 ORD-1004",
            "不需要再次退一次款，只查询订单 ORD-1004",
            "不要给订单 ORD-1004 再退一次款",
            "不用给订单ORD-1004再退一次款",
            "无需办理退回货款，只查询订单 ORD-1001",
        ]:
            with self.subTest(text=text):
                reply = self.model._decide([HumanMessage(content=text)])
                self.assertEqual(reply.tool_calls[0]["name"], "get_order")


if __name__ == "__main__":
    unittest.main()
