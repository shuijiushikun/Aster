"""T4 routing contracts and business effects, without running the full suite."""

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import func, select

from app import redis as shared
from app.agent import chat_graph, graph
from app.agent.contracts import ExchangeProposal, IntentDecision
from app.agent.runner import run_agent_turn
from app.agent.runtime import ModelCalls, TurnRuntime
from app.agent.specialists import compile_specialist
from app.agent.streaming import GraphEvents
from app.auth import Principal
from app.config import get_settings
from app.db import seed
from app.db import session as db
from app.db.models import AfterSalesTicket, Conversation, Message, Refund, utcnow
from app.mcp.schemas import OrderInfo, OrderPage, ProductInfo, ReadResult, failure
from app.mcp.tools import build_commerce_tools
from app.rag.schemas import Evidence, RetrievalResult


def ai(content="", *, call=None, usage=True):
    message = AIMessage(
        content=content,
        tool_calls=[{"name": call[0], "args": call[1], "id": str(uuid4())}] if call else [],
    )
    if usage:
        message.usage_metadata = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    return message


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


def intent(kind, *, order=None, needs_order=False, ticket=None):
    return ai(
        IntentDecision(
            intent=kind,
            order_id=order,
            needs_order=needs_order,
            needs_knowledge=kind in ("knowledge", "exchange"),
            related_ticket_id=ticket,
        ).model_dump_json()
    )


def proposal(**updates):
    fields = dict(
        order_id="ORD-EX-1001",
        reason_type="defect",
        description="耳机左耳无声，希望更换同型号",
        requested_change="更换同型号耳机",
        evidence_document_ids=["KB-HP-01"],
        missing_information=[],
        recommended_action="request_review",
    )
    return ai(ExchangeProposal(**(fields | updates)).model_dump_json())


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


class Commerce:
    def __init__(self):
        self.calls = []

    async def call(self, principal: Principal, name, args):
        self.calls.append((principal, name, args))
        if name == "get_product":
            return ReadResult[ProductInfo](
                ok=True,
                data=ProductInfo(
                    product_id="PROD-HP-01",
                    model_code="HP-01",
                    name="耳机",
                    category="digital",
                    is_active=True,
                ),
            )
        if (
            name == "get_order"
            and args["order_id"] == "ORD-EX-1001"
            and principal.sub == "CUST-EX-001"
        ):
            return ReadResult[OrderInfo](
                ok=True,
                data=OrderInfo(
                    order_id=args["order_id"],
                    product_id="PROD-HP-01",
                    model_code="HP-01",
                    product_name="耳机",
                    category="digital",
                    amount="99",
                    status="delivered",
                    order_date=utcnow(),
                    delivered_date=utcnow(),
                    is_final_sale=False,
                    already_refunded=False,
                ),
            )
        if name == "list_my_orders":
            return ReadResult[OrderPage](ok=True, data=OrderPage(orders=[]))
        return failure("NOT_FOUND", "未找到订单")


@pytest.fixture
async def setup_chat(engine, monkeypatch):
    await seed.seed_exchange_samples()
    monkeypatch.setattr(get_settings(), "agent_v2_enabled", True)
    commerce = Commerce()
    monkeypatch.setattr(chat_graph, "build_commerce_tools", lambda: build_commerce_tools(commerce))

    async def retrieve(query, **scope):
        return evidence(scope.get("model_code"))

    monkeypatch.setattr(chat_graph, "retrieve_knowledge", retrieve)
    monkeypatch.setattr(graph, "get_fallback_model", lambda: None)
    return commerce


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


async def test_exchange_open_citations_subgraph_events_and_usage(setup_chat, monkeypatch):
    model = use_model(
        monkeypatch,
        intent("exchange", order="ORD-EX-1001", needs_order=True),
        ai(call=("get_order", {"order_id": "ORD-EX-1001"})),
        ai("订单已核实"),
        ai(call=("search_knowledge", {"query": "HP-01 左耳无声怎么换货"})),
        proposal(),
    )
    events = await turn("ORD-EX-1001 耳机左耳无声，我要换同型号")
    assert "本轮仅记录申请" in final(events)
    ticket_event = next(e for e in events if e["kind"] == "ticket_update")
    assert ticket_event["status"] == "OPEN"
    assert any(
        e["kind"] == "knowledge_sources" and e["sources"][0]["revision"] == "1" for e in events
    )
    steps = [e for e in events if e["kind"] == "step"]
    assert len([e for e in steps if e["step_type"] == "tool_result"]) == 2
    assert any("/tools" in e["node"] for e in steps if e["step_type"] == "tool_result")
    usages = [e for e in steps if e["step_type"] == "usage"]
    assert len(usages) == model.calls == 5  # no parent/subgraph double charge
    cid = events[0]["conversation_id"]
    async with db.SessionLocal() as session:
        ticket = await session.get(AfterSalesTicket, ticket_event["ticket_id"])
        assert ticket.evidence_refs[0]["chunk_index"] == 2
        assert (await session.get(Conversation, cid)).tokens_used == 60
        messages = list(
            await session.scalars(select(Message).where(Message.conversation_id == cid))
        )
        assert len(messages) == 2 and not messages[-1].content.startswith("{")
    assert await shared.customer_tokens_today("CUST-EX-001") == 60


@pytest.mark.parametrize("kind", ["knowledge", "order", "ticket_followup"])
async def test_read_intents_never_build_ticket(setup_chat, monkeypatch, kind):
    replies = [intent(kind)]
    if kind == "knowledge":
        replies += [
            ai(call=("search_knowledge", {"query": "换货申请政策"})),
            ai("请参阅 KB-HP-01，申请需人工审批。"),
        ]
    elif kind == "order":
        replies += [ai(call=("list_my_orders", {})), ai("没有近期订单"), ai("没有查询到近期订单。")]
    use_model(monkeypatch, *replies)
    events = await turn("查询售后信息")
    assert final(events) and await count_tickets() == 0


@pytest.mark.parametrize("text", ["耳机坏了，我要换货", "ORD-EX-1001 和 ORD-EX-1002 都换货"])
async def test_missing_or_ambiguous_order_requests_clarification(setup_chat, monkeypatch, text):
    use_model(monkeypatch, intent("exchange", order="ORD-EX-1001", needs_order=True))
    events = await turn(text)
    assert "唯一订单号" in final(events)
    assert setup_chat.calls == [] and await count_tickets() == 0


async def test_router_parse_failure_is_safe_and_usage_is_kept(setup_chat, monkeypatch):
    use_model(monkeypatch, ai('{"intent":"exchange","approved":true}'))
    events = await turn("申请换货")
    assert "暂时无法准确理解" in final(events)
    assert len([e for e in events if e.get("step_type") == "usage"]) == 1
    assert await count_tickets() == 0


@pytest.mark.parametrize("bad", ["order", "citation"])
async def test_proposal_cannot_change_verified_order_or_invent_citations(
    setup_chat, monkeypatch, bad
):
    use_model(
        monkeypatch,
        intent("exchange", order="ORD-EX-1001", needs_order=True),
        ai(call=("get_order", {"order_id": "ORD-EX-1001"})),
        ai("已查到"),
        ai(call=("search_knowledge", {"query": "左耳无声换货"})),
        proposal(
            **(
                {"order_id": "ORD-EX-1002"}
                if bad == "order"
                else {"evidence_document_ids": ["FAKE-DOC"]}
            )
        ),
    )
    assert "尚未提交" in final(await turn("ORD-EX-1001 左耳无声换同型号"))
    assert await count_tickets() == 0


async def test_order_ownership_failure_does_not_retrieve_or_write(setup_chat, monkeypatch):
    use_model(
        monkeypatch,
        intent("exchange", order="ORD-EX-1001", needs_order=True),
        ai(call=("get_order", {"order_id": "ORD-EX-1001"})),
        ai("查不到"),
    )
    assert "未能核实" in final(await turn("ORD-EX-1001 换货", customer="CUST-EX-002"))
    assert await count_tickets() == 0


async def test_product_scope_comes_from_mcp_not_model(setup_chat, monkeypatch):
    scopes = []

    async def retrieve(query, **scope):
        scopes.append(scope)
        return evidence()

    monkeypatch.setattr(chat_graph, "retrieve_knowledge", retrieve)
    use_model(
        monkeypatch,
        intent("knowledge", needs_order=True),
        ai(call=("get_product", {"model_code": "HP-01"})),
        ai("型号已核实"),
        ai(call=("search_knowledge", {"query": "HP-01 左耳无声"})),
        ai("先重置耳机，参考 KB-HP-01。"),
    )
    await turn("HP-01 左耳无声怎么办")
    assert scopes == [{"model_code": "HP-01", "category": "digital"}]
    assert await count_tickets() == 0


async def test_unverified_explicit_scope_never_falls_back_to_generic_knowledge(
    setup_chat, monkeypatch
):
    async def no_retrieve(*args, **kwargs):
        raise AssertionError("unverified product must not retrieve")

    monkeypatch.setattr(chat_graph, "retrieve_knowledge", no_retrieve)
    # Router forgot needs_order; the backend still recognises the explicit ID.
    use_model(
        monkeypatch,
        intent("knowledge", order="ORD-EX-2001"),
        ai(call=("get_order", {"order_id": "ORD-EX-2001"})),
        ai("未找到"),
    )
    assert "核实型号" in final(await turn("ORD-EX-2001 耳机怎么排查"))
    assert await count_tickets() == 0


async def test_insufficient_knowledge_never_creates_ticket(setup_chat, monkeypatch):
    async def retrieve(query, **kwargs):
        return RetrievalResult(
            query=query,
            model_code="HP-01",
            status="insufficient_evidence",
            reason="no_relevant_evidence",
        )

    monkeypatch.setattr(chat_graph, "retrieve_knowledge", retrieve)
    use_model(
        monkeypatch,
        intent("exchange", order="ORD-EX-1001", needs_order=True),
        ai(call=("get_order", {"order_id": "ORD-EX-1001"})),
        ai("已核实"),
        ai(call=("search_knowledge", {"query": "换货"})),
    )
    assert "尚未提交" in final(await turn("ORD-EX-1001 我要换货"))
    assert await count_tickets() == 0


async def test_unknown_usage_is_explicit_and_failed_later_calls_keep_prior_usage(
    setup_chat, monkeypatch
):
    use_model(
        monkeypatch,
        intent("knowledge"),
        ai(call=("search_knowledge", {"query": "售后政策"}), usage=False),
        RuntimeError("provider failed"),
    )
    events = await turn("换货政策是什么")
    usage = [e["payload"] for e in events if e.get("step_type") == "usage"]
    assert [u["status"] for u in usage] == ["reported", "unknown", "unknown"]
    assert usage[1]["input_tokens"] is None
    assert events[-1]["kind"] == "error"
    assert await shared.customer_tokens_today("CUST-EX-001") == 12


async def test_v2_refund_adapter_preserves_real_tool_result_and_accounting(setup_chat, monkeypatch):
    use_model(
        monkeypatch,
        intent("refund", order="ORD-1001", needs_order=True),
        ai(call=("issue_refund", {"order_id": "ORD-1001", "reason": "客户申请退款"})),
        ai("退款已批准。"),
    )
    events = await turn("ORD-1001 帮我退款", customer="CUST-001")
    assert "退款已批准" in final(events)
    assert not any(e.get("step_type") == "output_correction" for e in events)
    assert len([e for e in events if e.get("step_type") == "usage"]) == 3
    async with db.SessionLocal() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Refund)
                .where(Refund.order_id == "ORD-1001", Refund.decision == "approved")
            )
            == 1
        )


async def test_order_tool_budget_is_three_even_for_looping_model():
    from langchain_core.tools import tool

    executed = []

    @tool
    async def read_order() -> str:
        """Read only."""
        executed.append(True)
        return "ok"

    model = Model(*(ai(call=("read_order", {})) for _ in range(4)))
    result = await compile_specialist(ModelCalls(model), [read_order], "order_agent").ainvoke(
        {"messages": [HumanMessage("订单")], "calls": 0},
        config={"configurable": {"turn_runtime": TurnRuntime()}},
    )
    assert result["calls"] == len(executed) == model.calls == 3


def test_parent_subgraph_events_are_deduplicated_and_json_never_final():
    adapter = GraphEvents(v2=True)
    message = ai('{"intent":"order"}')
    message.id = "one"
    assert len(list(adapter.adapt((("order_agent:123",), {"agent": {"messages": [message]}})))) == 1
    assert list(adapter.adapt(((), {"order_agent": {"order_messages": [message]}}))) == []
    tool_msg = ToolMessage("ok", tool_call_id="call", name="get_order", id="two")
    assert (
        next(adapter.adapt((("order_agent:123",), {"tools": {"messages": [tool_msg]}})))[0]
        == "message"
    )


def test_query_reply_cannot_claim_exchange_success_or_trust_status_without_receipt():
    assert chat_graph.claims_exchange_success("换货已受理，请等待后续通知。")
    assert chat_graph.claims_exchange_success("Your exchange has been approved.")
    assert not chat_graph.claims_exchange_success("换货尚未受理，审批后才能处理。")
    text = chat_graph.ticket_reply({"id": "TKT-1", "order_id": "ORD-1", "status": "COMPLETED"})
    assert "凭证尚待核实" in text and "受理已完成" not in text
