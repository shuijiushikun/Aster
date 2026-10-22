"""Opt-in provider smoke: python -m scripts.deepseek_smoke --live.

普通回复、工具往返、JSON mode与流式输出使用专用DeepSeek密钥。
超时/429/异常的可重复验证在tests/test_deepseek.py中，不故意请求线上失败。
官方接口：https://api-docs.deepseek.com/guides/tool_calls/
JSON要求：https://api-docs.deepseek.com/guides/json_mode/
"""

import argparse
import asyncio
import json
from typing import Literal

from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel

from app.agent.llm import _build
from app.config import get_settings


class SmokeReply(BaseModel):
    status: Literal["ok"]


SAMPLE_TOOL = {
    "type": "function",
    "function": {
        "name": "sample_order",
        "description": "读取本测试的静态演示订单状态。",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
}


async def smoke():
    model = _build("deepseek", max_retries=0)
    results = []
    step = "reply"
    try:
        reply = await model.ainvoke("本测试只需回复：你好")
        assert reply.content
        results.append({"case": step, "status": "passed"})
        step = "tool_round_trip"
        tool_model = model.bind_tools([SAMPLE_TOOL], tool_choice="sample_order")
        question = HumanMessage(content="调用sample_order查询静态演示订单smoke-1的状态。")
        call = await tool_model.ainvoke([question])
        assert len(call.tool_calls) == 1 and call.tool_calls[0]["name"] == "sample_order"
        assert call.tool_calls[0]["args"] == {"order_id": "smoke-1"}
        final = await model.ainvoke(
            [
                question,
                call,
                ToolMessage(
                    content='{"status":"delivered"}', tool_call_id=call.tool_calls[0]["id"]
                ),
            ]
        )
        assert final.content
        results.append({"case": step, "status": "passed"})
        step = "json_mode"
        structured = await model.with_structured_output(SmokeReply, method="json_mode").ainvoke(
            '请仅返回JSON，示例：{"status":"ok"}。status必须为ok。'
        )
        assert structured.status == "ok"
        results.append({"case": step, "status": "passed"})
        step = "stream"
        chunks = [chunk async for chunk in model.astream("请回复：你好")]
        assert any(chunk.content for chunk in chunks)
        results.append(
            {
                "case": step,
                "status": "passed",
                "usage_available": any(chunk.usage_metadata for chunk in chunks),
            }
        )
    except Exception as error:
        # 不打印原始响应、认证头或可能含连接配置的异常消息。
        results.append({"case": step, "status": "failed", "error_type": type(error).__name__})
        print(json.dumps({"status": "failed", "cases": results}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {"status": "passed", "model": get_settings().deepseek_model, "cases": results},
            ensure_ascii=False,
        )
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="call the configured DeepSeek API (uses API quota)"
    )
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"status": "skipped", "reason": "pass --live to make real API calls"}))
        return 0
    if not get_settings().deepseek_api_key.strip():
        print(json.dumps({"status": "skipped", "reason": "DEEPSEEK_API_KEY is not configured"}))
        return 0
    return asyncio.run(smoke())


if __name__ == "__main__":
    raise SystemExit(main())
