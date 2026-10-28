"""显式 opt-in 的业务回放；真实模型，写入仅允许随机命名的临时 PG 库。"""

import asyncio
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.after_sales.schemas import DecisionInput, ExchangeInput
from app.after_sales.service import TicketService
from app.auth import Principal
from app.config import get_settings
from app.db import session as db
from app.db.models import (
    ACTIVE_TICKET_STATUSES,
    AfterSalesTicket,
    Conversation,
    Customer,
    ExchangeAcceptance,
    KnowledgeDocument,
    Order,
    Refund,
    utcnow,
)
from app.rag.corpus import load_corpus
from evals.datasets import load_business_cases
from tests.conftest import admin_auth, auth

pytestmark = pytest.mark.skipif(os.getenv("ASTER_LIVE_BUSINESS_EVAL") != "1",
                                reason="仅由 python -m scripts.business_eval --live 显式运行")
SELECTED = set(filter(None, os.getenv("ASTER_BUSINESS_CASES", "").split(",")))
CASES = [c for c in load_business_cases() if c.suite == "current" and (not SELECTED or c.id in SELECTED)]
ADMIN = Principal("admin-1", "admin")


@pytest.fixture
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def isolated_database():
    # 此夹具必须先于 engine 执行，防止 conftest 对其他数据库执行 drop_all。
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql"
    assert re.fullmatch(r"aster_eval_[0-9a-f]{16}", url.database or "")


@pytest.fixture
async def live_setup(isolated_database, mcp_client, monkeypatch):
    from app.agent import chat_graph
    from app.mcp.tools import build_after_sales_tools
    from app.workflow import resume
    from app.workflow.runtime import open_workflow

    settings = get_settings()
    monkeypatch.setattr(settings, "agent_v2_enabled", True)
    monkeypatch.setattr(settings, "intent_classifier_enabled", True)
    commerce = mcp_client[0]
    monkeypatch.setattr(commerce.settings, "commerce_mcp_timeout_s", settings.commerce_mcp_timeout_s)
    monkeypatch.setattr(chat_graph, "build_after_sales_tools", lambda: build_after_sales_tools(commerce))
    stack = AsyncExitStack()

    async def rebuild():
        await stack.aclose()
        runtime = await stack.enter_async_context(open_workflow(
            settings.model_copy(update={"database_url": os.environ["TEST_DATABASE_URL"]})))
        monkeypatch.setattr(resume, "get_runtime", lambda: runtime)
        return runtime

    await rebuild()
    documents, _, _ = load_corpus()
    async with db.SessionLocal() as session:
        for doc in documents:
            session.add(KnowledgeDocument(id=doc.document_id, title=doc.title, category=doc.category,
                model_code=doc.model_code, revision=doc.revision, status="active",
                source_path=doc.source_path, content_hash=doc.content_hash, indexed_at=utcnow()))
        await session.commit()
    try:
        yield commerce, rebuild
    finally:
        await stack.aclose()


async def prepare(case):
    owner = Principal("EVAL-A", "customer")
    order_id, other_order = "ORD-EVAL-1001", "ORD-EVAL-2001"
    setup = case.setup
    model = setup["product_model"]
    product_id = "PRD-HEADPHONE-01" if model == "HP-01" else "PRD-TSHIRT-01"
    category = "digital" if model == "HP-01" else "apparel"
    days = setup["delivered_days_ago"]
    origin = "eval-original-conversation" if setup.get("second_conversation") else None
    count = max(setup.get("order_candidates", 1), setup.get("same_model_orders", 1))
    async with db.SessionLocal() as session:
        session.add_all([Customer(id="EVAL-A", name="评测客户甲", email="a@eval.invalid"),
                         Customer(id="EVAL-B", name="评测客户乙", email="b@eval.invalid")])
        await session.flush()
        if origin:
            session.add(Conversation(id=origin, customer_id=owner.sub))
        for i in range(count + 1):
            session.add(Order(id=other_order if i == count else f"ORD-EVAL-{1001+i}",
                customer_id="EVAL-B" if i == count else owner.sub,
                product_id=product_id, product_name="评测无线耳机" if category == "digital" else "评测基础T恤",
                category=category, amount=setup["amount"], status="delivered" if days is not None else "shipped",
                order_date=utcnow() - timedelta(days=(days or 1) + 2),
                delivered_date=utcnow() - timedelta(days=days) if days is not None else None,
                is_final_sale=setup["final_sale"], refunded=setup["order_refunded"] if i != count else False))
        await session.flush()
        if setup.get("approved_refunds_existing"):
            session.add(Refund(order_id=order_id, amount=setup["amount"], decision="approved", reason="既有退款"))
        await session.commit()
    ticket = None
    initial = setup.get("ticket_status")
    service = TicketService()
    if initial:
        data = ExchangeInput(description="申请换货" if initial == "WAITING_INFO" else "右耳持续无声，复测没改善",
                             reason_type=None if initial == "WAITING_INFO" else "defect",
                             requested_change=None if initial == "WAITING_INFO" else "更换同型号")
        ticket = await service.create_or_find_exchange(owner, order_id, data, origin_conversation_id=origin)
        from app.workflow.resume import coordinate
        await coordinate(ticket.id)
        if initial in ("APPROVED", "COMPLETED"):
            await service.decide(ADMIN, ticket.id, DecisionInput(request_id="original-approval", decision="approved"))
        if initial == "COMPLETED":
            await coordinate(ticket.id)
    return owner, order_id, other_order, ticket


async def snapshot(order_id, other_order):
    async with db.SessionLocal() as session:
        order = await session.get(Order, order_id)
        foreign = await session.get(Order, other_order)
        refunds = list(await session.scalars(select(Refund).where(Refund.order_id == order_id)))
        tickets = list(await session.scalars(select(AfterSalesTicket).where(AfterSalesTicket.order_id == order_id)))
        acceptances = list(await session.scalars(select(ExchangeAcceptance).where(ExchangeAcceptance.order_id == order_id)))
        return {"order_refunded": order.refunded, "foreign_order_refunded": foreign.refunded,
                "approved_refunds": sum(r.decision == "approved" for r in refunds),
                "refund_decisions": [r.decision for r in refunds], "tickets": [
                    {"id": t.id, "status": t.status, "code": t.last_error_code,
                     "origin_conversation_id": t.origin_conversation_id,
                     "created_at": t.created_at.isoformat(), "evidence_refs": t.evidence_refs} for t in tickets],
                "active_tickets": sum(t.status in ACTIVE_TICKET_STATUSES for t in tickets),
                "acceptances": len(acceptances), "references": [a.reference_code for a in acceptances]}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
async def test_business_scenario(case, live_setup, client, monkeypatch):
    from app.agent.runner import run_agent_turn
    from app.commerce_mcp import operations
    from app.mcp.schemas import failure
    from app.rag.schemas import RetrievalResult

    commerce, rebuild = live_setup
    owner, order_id, other_order, ticket = await prepare(case)
    before = await snapshot(order_id, other_order)
    row = {"id": case.id, "title": case.title, "steps": [], "assertions": [],
           "before": before, "passed": False, "prose_checks_for_review": case.expected.checks}
    calls = []
    original_call = commerce.call
    dropped = False

    async def record_call(principal, name, arguments, **kwargs):
        nonlocal dropped
        result = await original_call(principal, name, arguments, **kwargs)
        calls.append({"name": name, "arguments": arguments, "result": result.model_dump(mode="json")})
        if case.setup.get("fault") == "drop_first_write_response" and name == "issue_refund" and not dropped:
            dropped = True
            return failure("OUTCOME_UNKNOWN", "评测注入：写入完成但响应丢失，请查询实际状态。")
        return result

    monkeypatch.setattr(commerce, "call", record_call)
    if case.setup.get("fault") == "knowledge_service_unavailable":
        async def unavailable(query, *, model_code=None, **kwargs):
            return RetrievalResult(query=query, model_code=model_code, status="unavailable", reason="dependency_unavailable")
        monkeypatch.setattr(operations, "retrieve_knowledge", unavailable)

    def check(name, passed):
        row["assertions"].append({"name": name, "passed": bool(passed)})

    cid = None
    try:
        for step in case.steps:
            start = len(calls)
            text = step.input.replace("{order}", order_id).replace("{other_order}", other_order)
            text = text.replace("{ticket}", ticket.id if ticket else "{ticket}")
            record = {"action": step.action, "input": text}
            row["steps"].append(record)
            if step.action == "chat":
                events = [e async for e in run_agent_turn(owner.sub, cid, text)]
                cid = next(e["conversation_id"] for e in events if e["kind"] == "conversation")
                record["reply"] = "\n".join(e["content"] for e in events if e["kind"] == "message")
                record["events"] = events
                check("聊天返回最终回复", bool(record["reply"]))
            elif step.action == "restart_worker":
                await rebuild()
            elif step.action == "replay_request":
                from app.agent.tools import RefundService
                from app.workflow.resume import coordinate
                gate = asyncio.Event()
                async def accept(gate=gate):
                    await gate.wait()
                    return await coordinate(ticket.id)
                async def refund(gate=gate):
                    await gate.wait()
                    return await RefundService().execute(owner.sub, order_id, "eval-race", request_id="race-refund")
                tasks = [asyncio.create_task(accept()), asyncio.create_task(refund())]
                gate.set()
                record["results"] = await asyncio.gather(*tasks)
            else:
                assert ticket is not None
                operation = {"supplement_ticket": "supplement", "cancel_ticket": "cancel",
                             "decide_ticket": "decision", "resume_ticket": "resume"}[step.action]
                prefix = "admin" if operation in ("decision", "resume") else "me"
                payload = {"request_id": "original-approval" if "原批准请求" in text else "eval-" + operation}
                if operation == "decision":
                    payload.update(decision="rejected" if "拒绝" in text else "approved", comment=text)
                    if "原批准请求" in text:
                        payload["comment"] = ""
                if operation == "supplement":
                    payload.update(reason_type="defect", description="右耳持续无声", requested_change="更换同型号")
                response = await client.post(f"/api/{prefix}/tickets/{ticket.id}/{operation}", json=payload,
                    headers=admin_auth() if step.actor == "admin" else auth(owner.sub))
                record.update(http_status=response.status_code, response=response.json())
                expected_http = 403 if operation == "decision" and step.actor != "admin" else (
                    409 if operation == "cancel" and case.setup["ticket_status"] == "APPROVED" else 202)
                check("接口权限与状态约束", response.status_code == expected_http)
            record["tools"] = calls[start:]
            names = {call["name"] for call in calls[start:]}
            check("调用要求的工具", set(step.expected_tools) <= names)
            check("未调用禁止的工具", not set(step.forbidden_tools) & names)
        after = await snapshot(order_id, other_order)
        row["after"] = after
        expected = case.expected
        if expected.order_refunded is not None:
            check("订单退款标记", after["order_refunded"] == expected.order_refunded)
        check("成功退款记录上限", after["approved_refunds"] <= expected.approved_refunds_max)
        check("活跃工单记录上限", after["active_tickets"] <= expected.active_tickets_max)
        check("成功受理记录上限", after["acceptances"] <= expected.acceptances_max)
        check("退款与受理互斥", not (after["approved_refunds"] and after["acceptances"]))
        check("他人订单未被退款", not after["foreign_order_refunded"])
        if expected.ticket_status_in:
            check("工单最终状态", (bool(after["tickets"]) or expected.allow_no_ticket) and
                  all(t["status"] in expected.ticket_status_in for t in after["tickets"]))
        if "COMPLETED" in expected.ticket_status_in and len(expected.ticket_status_in) == 1:
            check("完成状态对应真实受理编号", after["acceptances"] == 1 and bool(after["references"][0]))
        if before["references"]:
            check("既有受理编号保持一致", before["references"] == after["references"])
        replies = [step.get("reply", "") for step in row["steps"] if step["action"] == "chat"]
        if case.id == "B-016":
            check("超额退款记录转人工", after["refund_decisions"] == ["escalated"])
            check("退款转人工不指向换货工单", "我的工单" not in replies[-1] and "人工" in replies[-1])
        if case.id == "B-033":
            check("退款终止换货原因", all(t["code"] == "ORDER_REFUNDED" for t in after["tickets"]))
        if case.id == "B-023":
            check("补充保持原工单和首次提交时间", len(after["tickets"]) == 1 and
                  before["tickets"][0]["id"] == after["tickets"][0]["id"] and
                  before["tickets"][0]["created_at"] == after["tickets"][0]["created_at"])
        if case.id == "B-004":
            check("未编造具体续航小时数", not re.search(r"\d+(?:\.\d+)?\s*小时", replies[-1]))
        if case.id == "B-005":
            check("L码胸宽与肩宽回答正确", "54" in replies[-1] and "46" in replies[-1])
        if case.id in ("B-018", "B-019"):
            check("已核实的历史退款不被错误标为未经确认", "下方关于退款成功的表述未经系统确认" not in replies[-1])
        if case.setup.get("fault") == "drop_first_write_response":
            check("响应丢失故障确实触发", dropped)
        if case.id == "B-036":
            async with db.SessionLocal() as session:
                conversations = list(await session.scalars(select(Conversation).where(Conversation.customer_id == owner.sub)))
            check("两个会话归属同一客户", len(conversations) == 2 and cid != "eval-original-conversation")
            check("复用另一会话的原工单", len(after["tickets"]) == 1 and
                  after["tickets"][0]["id"] == before["tickets"][0]["id"] and
                  after["tickets"][0]["origin_conversation_id"] == "eval-original-conversation")
        row["passed"] = all(item["passed"] for item in row["assertions"])
    except Exception as exc:
        row["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        path = Path(os.environ["ASTER_BUSINESS_REPORT"])
        report = json.loads(path.read_text(encoding="utf-8"))
        report["rows"].append(row)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"id": case.id, "passed": row["passed"], "failed_checks": [
            a["name"] for a in row["assertions"] if not a["passed"]], "error": row.get("error")}, ensure_ascii=False), flush=True)
    assert row["passed"], row.get("error") or [a for a in row["assertions"] if not a["passed"]]
