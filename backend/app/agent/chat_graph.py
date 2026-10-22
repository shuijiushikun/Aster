"""Short-lived T4 chat graph. Approval/checkpoint workflows belong to T5."""

import json
import re
from typing import TypedDict

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from app.after_sales.schemas import EvidenceRef, ExchangeInput
from app.after_sales.service import TicketError, TicketService
from app.agent.context import assemble, current_text, grounded_id, identifiers
from app.agent.contracts import ExchangeProposal, IntentDecision
from app.agent.prompts import get_system_prompt
from app.agent.runtime import ModelCalls
from app.agent.specialists import compile_specialist
from app.agent.streaming import message_text
from app.agent.tools import _ctx
from app.auth import Principal
from app.config import get_settings
from app.db import session as db
from app.db.models import Order, Product
from app.mcp.schemas import RESULT_TYPES
from app.mcp.tools import build_commerce_tools
from app.rag.schemas import RetrievalResult


class ChatState(TypedDict, total=False):
    messages: list
    intent: dict
    order_facts: dict
    knowledge_evidence: dict
    proposal: dict
    ticket_result: dict
    final_response: str
    safe_response: str
    order_messages: list
    knowledge_messages: list
    router_messages: list
    proposal_messages: list
    workflow_result: dict


_STATUSES = {
    "OPEN": "已提交，待工作流处理",
    "WAITING_INFO": "等待补充信息",
    "PENDING_REVIEW": "等待人工审批",
    "APPROVED": "等待受理，尚未完成换货",
    "COMPLETED": "模拟换货受理已完成",
    "REJECTED": "申请已拒绝",
    "CANCELLED": "已取消",
    "FAILED_RETRYABLE": "处理失败，等待重试",
    "FAILED_TERMINAL": "处理失败，需要人工处置",
}


def ticket_reply(ticket):
    status = _STATUSES.get(ticket["status"], "请联系人工客服核实状态")
    if ticket["status"] == "COMPLETED" and not ticket.get("reference_code"):
        status = "工单记录为完成，受理凭证尚待核实"
    return f"工单 {ticket['id']}（订单 {ticket['order_id']}）的当前状态：{status}。"


async def retrieve_knowledge(query, *, model_code=None, category=None):
    from app.rag.retriever import KnowledgeRetriever
    from app.rag.store import open_store

    try:
        async with open_store(get_settings()) as (store, client):
            return await KnowledgeRetriever(store, client).retrieve(
                query, model_code=model_code, category=category
            )
    except Exception:
        return RetrievalResult(
            query=query, model_code=model_code, status="unavailable", reason="service_unavailable"
        )


async def structured(caller, schema, messages, config, node):
    prompt = SystemMessage(
        "只返回一个符合以下 JSON Schema 的 JSON 对象，不要 Markdown，不要额外字段：\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    raw = await caller.call([prompt, *messages], config, node=node, json_mode=True)
    # There is no unbounded repair loop. Bad output goes to a safe clarification.
    return schema.model_validate_json(message_text(raw)), raw


def _tool_results(messages):
    for message in messages:
        if isinstance(message, ToolMessage) and message.name in RESULT_TYPES:
            try:
                yield message.name, RESULT_TYPES[message.name].model_validate_json(message.content)
            except (ValidationError, ValueError):
                continue


def compile_chat_graph(model, fallback=None):
    caller = ModelCalls(model, fallback)

    async def load_context(state: ChatState):
        return {"order_facts": {}, "knowledge_evidence": {}, "ticket_result": {}, "proposal": {}}

    async def intent_router(state: ChatState, config: RunnableConfig):
        instruction = (
            "识别客户当前请求。退款=refund；明确申请换货=exchange；只问政策、说明、故障排查=knowledge；"
            "只查订单=order；查询已有换货工单进度=ticket_followup；其他=other。"
            "咨询换货政策不代表申请换货。承接上文时结合客户最近消息，不能因为历史申请自动再次建单。"
            "需要查询具体订单/明确型号知识时 needs_order=true；通用知识为 false。"
            "exchange 必须 needs_order/needs_knowledge=true。不能虚构 ID。"
        )
        try:
            decision, raw = await structured(
                caller,
                IntentDecision,
                assemble(state["messages"], instruction),
                config,
                "intent_router",
            )
            return {"intent": decision.model_dump(), "router_messages": [raw]}
        except Exception:
            # Keep infrastructure details out of the customer response.
            return {
                "intent": {"intent": "other"},
                "safe_response": "暂时无法准确理解该请求。请说明要查询订单、咨询政策、申请退款还是换货。",
            }

    async def order_agent(state: ChatState, config: RunnableConfig):
        decision = state["intent"]
        order_id = grounded_id(state["messages"], decision.get("order_id"), "ORD")
        explicit_ids = identifiers(state["messages"], "ORD")
        if len(explicit_ids) > 1 or (decision["intent"] == "exchange" and not order_id):
            return {"safe_response": "请提供这次要处理的唯一订单号，以便核实订单和商品型号。"}
        # Product codes are candidates only; get_product must verify them first.
        codes = (
            set(
                re.findall(
                    r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9_-])",
                    current_text(state["messages"]),
                )
            )
            - explicit_ids
        )

        def allowed(name, args):
            if name in ("get_order", "get_exchange_policy_facts"):
                return order_id is not None and args.get("order_id") == order_id
            if name == "get_product":
                return args.get("model_code") in codes
            return name == "list_my_orders"

        specialist = compile_specialist(
            caller, build_commerce_tools(), "order_agent", allowed=allowed
        )
        prompt = (
            "你是订单只读查询代理，只能调用商城 MCP 工具，不能退款、建单、批准或承诺换货。"
            "最多执行3个工具。存在确认订单号时必须 get_order；没有订单号且有型号时 get_product；"
            "否则 list_my_orders。读取成功后停止，不要重复查询，不要猜订单号。工具失败就停止。"
        )
        result = await specialist.ainvoke(
            {
                "messages": assemble(
                    state["messages"], prompt, order_id=order_id, model_candidates=sorted(codes)
                ),
                "calls": 0,
            },
            config=config,
        )
        facts = {"scope_required": bool(order_id or codes or decision.get("needs_order"))}
        for name, outcome in _tool_results(result["messages"]):
            if not outcome.ok:
                facts["error"] = outcome.error.code
            elif name == "get_order" and outcome.data.order_id == order_id:
                facts["order"] = outcome.data.model_dump(mode="json")
            elif (
                name == "get_product"
                and outcome.data.model_code in codes
                and outcome.data.is_active
            ):
                facts["product"] = outcome.data.model_dump(mode="json")
            elif name == "list_my_orders":
                facts["orders"] = outcome.data.model_dump(mode="json")["orders"]
        if decision["intent"] == "exchange" and not facts.get("order"):
            return {
                "order_facts": facts,
                "order_messages": result["messages"],
                "safe_response": "暂时未能核实该订单的归属和商品信息，请确认订单号或稍后重试。",
            }
        return {"order_facts": facts, "order_messages": result["messages"]}

    async def knowledge_agent(state: ChatState, config: RunnableConfig):
        if state.get("safe_response"):
            return {}
        facts = state.get("order_facts", {})
        scope = facts.get("order") or facts.get("product") or {}
        if (state["intent"].get("needs_order") or facts.get("scope_required")) and not scope:
            return {"safe_response": "请提供订单号或明确的商品型号，我需要核实型号后查询对应说明。"}
        if scope and not scope.get("model_code"):
            return {"safe_response": "该订单暂未关联可核实的商品型号，请补充型号或联系人工客服。"}
        evidence = None

        @tool
        async def search_knowledge(query: str) -> str:
            """检索售后知识。只提供自然语言问题；可信商品范围由后端固定，不能自行修改。"""
            nonlocal evidence
            # A specialist cannot use multiple tools to change the fixed scope or
            # replace the original request with an unrelated query.
            if evidence is None:
                query = query.strip()[:2000] or current_text(state["messages"])
                evidence = await retrieve_knowledge(
                    query, model_code=scope.get("model_code"), category=scope.get("category")
                )
            return evidence.model_dump_json()

        specialist = compile_specialist(
            caller, [search_knowledge], "knowledge_agent", finish_after_tools=True
        )
        prompt = "你是知识检索代理。必须调用一次 search_knowledge，以客户当前完整问题查询。不要自行回答或改变商品型号，不要写入业务数据。"
        result = await specialist.ainvoke(
            {"messages": assemble(state["messages"], prompt, scope=scope), "calls": 0},
            config=config,
        )
        if evidence is None:
            evidence = RetrievalResult(
                query=current_text(state["messages"]),
                model_code=scope.get("model_code"),
                status="insufficient_evidence",
                reason="no_search",
            )
        return {
            "knowledge_evidence": evidence.model_dump(mode="json"),
            "knowledge_messages": result["messages"],
        }

    async def gather_facts(state: ChatState):
        if state.get("safe_response"):
            return {}
        if not state.get("order_facts", {}).get("order"):
            return {"safe_response": "请先提供唯一且可核实的本人订单号。"}
        if state.get("knowledge_evidence", {}).get("status") != "ok":
            return {
                "safe_response": "暂时没有检索到足够的对应商品资料，尚未提交换货申请。请补充故障或尺码问题，或联系人工客服。"
            }
        return {}

    async def proposal(state: ChatState, config: RunnableConfig):
        if state.get("safe_response"):
            return {}
        try:
            value, raw = await structured(
                caller,
                ExchangeProposal,
                assemble(
                    state["messages"],
                    "整理客户明确提出的换货诉求，不得编造原因或目标。未说明的 reason_type/目标留 null。"
                    "只能引用本轮真实文档 ID。该提案不是审批结果，不能批准或受理换货。",
                    order=state["order_facts"]["order"],
                    evidence=state["knowledge_evidence"],
                ),
                config,
                "proposal",
            )
            return {"proposal": value.model_dump(), "proposal_messages": [raw]}
        except Exception:
            return {
                "safe_response": "暂时未能整理出可靠的换货申请，请补充具体问题和希望更换的商品或尺码。"
            }

    async def policy_gate(state: ChatState, config: RunnableConfig):
        if state.get("safe_response"):
            return {}
        ctx = _ctx(config)
        value = ExchangeProposal.model_validate(state["proposal"])
        order = state["order_facts"]["order"]
        sources = state["knowledge_evidence"]["sources"]
        actual_ids = {source["document_id"] for source in sources}
        if (
            value.order_id != order["order_id"]
            or not value.evidence_document_ids
            or not set(value.evidence_document_ids) <= actual_ids
        ):
            return {"safe_response": "申请中的订单或知识引用未通过核验，尚未提交，请重新描述诉求。"}
        # Re-read the local authoritative product link; creation below rechecks
        # ownership and policy under the existing per-order transaction lock.
        async with db.SessionLocal() as session:
            row = await session.get(Order, order["order_id"])
            product = await session.get(Product, row.product_id) if row and row.product_id else None
            if (
                row is None
                or row.customer_id != ctx.verified_customer_id
                or product is None
                or product.model_code != order["model_code"]
                or product.category != order["category"]
            ):
                return {"safe_response": "订单或商品信息发生变化，尚未提交申请，请重新查询。"}
        refs = [
            EvidenceRef(
                document_id=s["document_id"],
                revision=s["revision"],
                chunk_index=s["chunk_index"],
                excerpt=s["excerpt"][:300],
            )
            for s in sources
            if s["document_id"] in value.evidence_document_ids
        ]
        try:
            ticket = await TicketService().create_or_find_exchange(
                Principal(ctx.verified_customer_id, "customer"),
                order["order_id"],
                ExchangeInput(
                    description=value.description,
                    reason_type=value.reason_type,
                    requested_change=value.requested_change,
                    evidence_refs=refs,
                ),
                origin_conversation_id=ctx.conversation_id,
                start_open=True,
            )
            from app.workflow.resume import coordinate

            workflow_result = await coordinate(ticket.id)
            detail = await TicketService().detail(
                Principal(ctx.verified_customer_id, "customer"), ticket.id
            )
            return {
                "ticket_result": {
                    "id": ticket.id,
                    "order_id": ticket.order_id,
                    "status": detail.status,
                    "reference_code": detail.reference_code,
                },
                "workflow_result": {"ticket_id": ticket.id, "state": workflow_result["state"]},
            }
        except TicketError as exc:
            return {"safe_response": exc.message}

    async def lookup_owned_ticket(state: ChatState, config: RunnableConfig):
        principal = Principal(_ctx(config).verified_customer_id, "customer")
        ticket_id = grounded_id(state["messages"], state["intent"].get("related_ticket_id"), "TKT")
        if identifiers(state["messages"], "TKT") and not ticket_id:
            return {"safe_response": "请确认这次要查询的唯一工单号。"}
        try:
            service = TicketService()
            if ticket_id:
                ticket = await service.detail(principal, ticket_id)
                return {"safe_response": ticket_reply(ticket.model_dump())}
            tickets = await service.list_tickets(principal, limit=5)
            return {
                "safe_response": "\n".join(ticket_reply(t.model_dump()) for t in tickets)
                if tickets
                else "当前没有可查询的换货工单。"
            }
        except TicketError as exc:
            return {"safe_response": exc.message}

    async def legacy_refund_adapter(state: ChatState, config: RunnableConfig):
        from app.agent.graph import _compile

        history = [
            message for message in state["messages"] if not isinstance(message, SystemMessage)
        ]
        result = await _compile(model, fallback).ainvoke(
            {"messages": [SystemMessage(get_system_prompt()), *history]}, config=config
        )
        text = message_text(result["messages"][-1])
        return {"safe_response": text, "order_messages": result["messages"]}

    async def reply(state: ChatState, config: RunnableConfig):
        if state.get("safe_response"):
            return {"final_response": state["safe_response"]}
        if state.get("ticket_result"):
            return {
                "final_response": ticket_reply(state["ticket_result"])
                + " 本轮仅记录申请，不代表审批通过或换货受理成功。"
            }
        intent = state["intent"]["intent"]
        if intent == "other":
            return {
                "final_response": "我可以协助查询 ACME 订单、售后知识、退款、换货申请与工单进度。请说明您需要哪项帮助。"
            }
        if intent == "knowledge" and state.get("knowledge_evidence", {}).get("status") != "ok":
            return {
                "final_response": "暂时没有足够的知识证据回答该问题。请补充商品型号或问题细节，或联系人工客服。"
            }
        if intent == "order" and not any(
            state.get("order_facts", {}).get(key) for key in ("order", "orders", "product")
        ):
            if state.get("order_facts", {}).get("orders") == []:
                return {"final_response": "您目前没有可查询的近期订单。"}
            return {"final_response": "暂时无法查询该订单。请核对订单号或稍后重试。"}
        raw = await caller.call(
            assemble(
                state["messages"],
                "用简洁简体中文回答当前查询，只能使用已核实事实/检索证据。金额为 USD。"
                "知识回答注明文档 ID。没有证据时明确说无法确认。你没有执行退款、提交申请、审批或受理，"
                "不得声称这些动作已完成。不要展示 JSON、工具参数或内部错误码。",
                order_facts=state.get("order_facts"),
                evidence=state.get("knowledge_evidence"),
            ),
            config,
            node="reply",
        )
        text = message_text(raw)
        if claims_exchange_success(text):
            text = "本轮仅查询信息，没有提交、批准或受理换货申请。请说明是否需要申请换货。"
        return {"messages": [raw], "final_response": text}

    def route(state):
        intent = state["intent"]["intent"]
        if intent == "refund":
            return "legacy_refund_adapter"
        if intent == "ticket_followup":
            return "lookup_owned_ticket"
        explicit_scope = bool(
            identifiers(state["messages"], "ORD")
            or re.search(r"\b[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+", current_text(state["messages"]))
        )
        if intent in ("order", "exchange") or (
            intent == "knowledge" and (state["intent"].get("needs_order") or explicit_scope)
        ):
            return "order_agent"
        return "knowledge_agent" if intent == "knowledge" else "reply"

    def after_order(state):
        if state.get("safe_response"):
            return "reply"
        return (
            "knowledge_agent" if state["intent"]["intent"] in ("exchange", "knowledge") else "reply"
        )

    graph = StateGraph(ChatState)
    for name, fn in (
        ("load_context", load_context),
        ("intent_router", intent_router),
        ("order_agent", order_agent),
        ("knowledge_agent", knowledge_agent),
        ("gather_facts", gather_facts),
        ("proposal", proposal),
        ("policy_gate", policy_gate),
        ("lookup_owned_ticket", lookup_owned_ticket),
        ("legacy_refund_adapter", legacy_refund_adapter),
        ("reply", reply),
    ):
        graph.add_node(name, fn)
    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "intent_router")
    graph.add_conditional_edges("intent_router", route)
    graph.add_conditional_edges("order_agent", after_order)
    graph.add_conditional_edges(
        "knowledge_agent",
        lambda state: "gather_facts" if state["intent"]["intent"] == "exchange" else "reply",
    )
    graph.add_edge("gather_facts", "proposal")
    graph.add_edge("proposal", "policy_gate")
    graph.add_edge("policy_gate", "reply")
    graph.add_edge("lookup_owned_ticket", "reply")
    graph.add_edge("legacy_refund_adapter", "reply")
    graph.add_edge("reply", END)
    return graph.compile()


def claims_exchange_success(text):
    # Query replies have no write result. Negated/conditional wording is allowed.
    for sentence in re.split(r"[。！？!?；;，,\n]", text):
        if re.search(
            r"未|没有|不代表|不能|尚未|如果|若|审批后|\b(?:not|cannot|if|pending)\b",
            sentence,
            re.IGNORECASE,
        ):
            continue
        if re.search(
            r"(?:换货|申请|工单).{0,16}(?:已提交|提交成功|已批准|已获批准|审批通过|批准成功|已受理|受理成功|已完成|完成换货)|(?:已|成功).{0,6}(?:为您|为你)?(?:提交|批准|受理|完成).{0,8}(?:换货|申请|工单)|\bexchange\b.{0,32}\b(?:approved|accepted|completed|submitted)\b",
            sentence,
            re.IGNORECASE,
        ):
            return True
    return False
