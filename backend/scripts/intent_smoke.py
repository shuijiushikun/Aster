"""只读本地分类验证：python -m scripts.intent_smoke --live，不调用业务工具。"""

import argparse
import asyncio
import json
import time

from langchain_core.messages import AIMessage, HumanMessage

from app.after_sales.intent import classify_intent
from app.agent.runtime import TurnRuntime
from app.config import get_settings

CASES = [
    ("商品知识", [], "HP-01 耳机的续航多长？", "query"),
    ("订单查询", [], "帮我查一下 ORD-DEMO-1005", "query"),
    ("退款资格", [], "ORD-DEMO-1001 能退款吗？", "query"),
    ("换货政策", [], "耳机换货需要什么条件？", "query"),
    ("故障咨询", [], "耳机左边没声音，怎么办？", "query"),
    ("否定申请", [], "不要退款，只查 ORD-DEMO-1001", "query"),
    ("取消意愿", [], "不换了，先放着吧", "query"),
    ("直接退款", [], "ORD-DEMO-1001 帮我退款", "refund"),
    ("礼貌退款", [], "ORD-DEMO-1001 可以帮我办理退款吗？", "refund"),
    ("口语退款", [], "这笔订单我不想要了，给我退掉吧", "refund"),
    ("直接换货", [], "ORD-DEMO-1005 耳机坏了，帮我换同型号", "exchange"),
    ("礼貌换货", [], "可以帮我把这件衣服换大一码吗？", "exchange"),
    ("改为换货", [], "不要退钱了，帮我换个新的", "exchange"),
    ("两种选择", [], "帮我退款或者换货都行", "clarify"),
    ("未选方案", [AIMessage("您要退款还是换货？")], "好的", "clarify"),
    ("确认退款", [HumanMessage("ORD-DEMO-1001 想退"), AIMessage("确认提交退款申请吗？")], "确认", "refund"),
    ("确认换货", [HumanMessage("ORD-DEMO-1005 换同型号"), AIMessage("确认提交换货申请吗？")], "就按这个办", "exchange"),
    ("确认查询", [AIMessage("需要先查询退款资格吗？")], "好的", "query"),
    ("旧请求新咨询", [HumanMessage("帮我退款"), AIMessage("请确认订单号。")], "先告诉我退款条件", "query"),
    ("否定确认", [AIMessage("确认提交退款申请吗？")], "先别操作，我再想想", "query"),
    # 以下改写不作为提示词示例，用于检查相邻表达。
    ("改写政策", [], "我就是看看退换货规定，别提交申请", "query"),
    ("改写故障", [], "新买的耳机连不上手机，有什么排查办法", "query"),
    ("改写退款", [], "麻烦给这笔订单办一下退货退款", "refund"),
    ("改写换货", [], "衣服太紧了，麻烦替我申请更换成 L 码", "exchange"),
    ("改写未选方案", [], "退也行换也行，你帮我决定", "clarify"),
    ("改写确认换货", [AIMessage("要我帮您发起换货申请吗？")], "麻烦了，办理吧", "exchange"),
    ("改写确认查询", [AIMessage("我可以先看看这个订单有没有退款资格。")], "嗯，先看看", "query"),
    ("改写两种确认", [AIMessage("退货退款还是直接换同款，您选哪种？")], "可以", "clarify"),
    ("改写取消后咨询", [HumanMessage("给我退款"), AIMessage("请提供订单号")], "不退了，耳机续航呢", "query"),
    ("改写办理问句", [], "我想退货，请问可以处理一下吗", "refund"),
    ("改写办理流程", [], "换货怎么办理啊", "query"),
    ("改写转退款", [], "不用换新的了，我要申请退钱", "refund"),
]


async def smoke():
    settings = get_settings()
    runtime = TurnRuntime()
    rows = []
    for name, history, text, expected in CASES:
        start = time.perf_counter()
        result = await classify_intent([*history, HumanMessage(text)],
                                      {"configurable": {"turn_runtime": runtime}})
        row = {"case": name, "expected": expected, "actual": result.intent,
               "source": result.source, "fallback_reason": result.fallback_reason,
               "seconds": round(time.perf_counter() - start, 2),
               "passed": result.source == "model" and result.intent == expected}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    passed = sum(row["passed"] for row in rows)
    print(json.dumps({"model": settings.intent_classifier_model, "passed": passed, "total": len(rows),
                      "reported_tokens": sum((u["input_tokens"] or 0) + (u["output_tokens"] or 0)
                                             for u in runtime.usage)}, ensure_ascii=False))
    return 0 if passed == len(rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="允许向已配置的本地 Ollama 发起真实分类请求")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实分类验证需要 --live；只使用虚构文本，不访问数据库")
    return asyncio.run(smoke())


if __name__ == "__main__":
    raise SystemExit(main())
