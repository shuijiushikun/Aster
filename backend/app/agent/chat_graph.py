"""一个售后 Agent 与 MCP 工具循环；审批和恢复仍由持久化工作流负责。"""

import json
import re
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Overwrite
from pydantic import ValidationError

from app.after_sales.intent import TurnIntent, classify_intent, requested_actions
from app.agent.answer_grounding import ground_answer
from app.agent.context import assemble, context_data, current_text, grounded_id, identifiers
from app.agent.prompts import get_after_sales_prompt
from app.agent.runtime import ModelCalls
from app.agent.streaming import message_text
from app.agent.tools import _ctx
from app.config import get_settings
from app.mcp.schemas import WRITE_TOOLS, failure
from app.mcp.tools import build_after_sales_tools


class ChatState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    tool_count: int
    order_facts: dict
    knowledge_evidence: dict
    ticket_result: dict
    workflow_result: dict
    final_response: str
    turn_intent: dict


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
    next_step = {
        "OPEN": "换货申请已保存，接下来需要核验申请材料。您可以在右侧“我的工单”查看进度；如果状态暂未更新，请刷新工单。",
        "WAITING_INFO": "申请已保存，还需要补齐材料。请在右侧“我的工单”中打开这笔申请，补充具体问题、换货原因和希望更换的型号或尺码，再提交材料。",
        "PENDING_REVIEW": "申请材料已进入人工审核，暂时无需重复提交。审核结果会显示在“我的工单”中；审批前如需撤回，可以在工单里取消申请。",
        "APPROVED": "管理员已批准这次申请，系统还需要完成最后的受理处理。请稍后刷新工单；如果一直停留在这里，可请管理员继续办理。",
        "FAILED_RETRYABLE": "申请和已提交的材料仍然保留，暂时遇到处理异常。请联系管理员点击“重试办理”，继续原有申请，无需重新建单。",
        "FAILED_TERMINAL": "这笔申请遇到了需要人工处理的问题，自动重试已停止。请联系管理员查看工单中的异常说明，确认后续处理方式。",
        "REJECTED": "这笔申请已经结束。您可以打开“我的工单”查看审批意见和处理记录，了解拒绝原因。",
        "CANCELLED": "这笔申请已取消，后续不会继续受理。如果仍希望换货，可以重新说明需求，系统会按新的申请时间核验条件。",
    }.get(ticket["status"], "请打开“我的工单”核对详细记录。")
    if ticket["status"] == "COMPLETED":
        next_step = (f"模拟受理单号：{ticket['reference_code']}。这次申请已处理结束，您可以在“我的工单”保留并查看记录。当前演示只生成受理记录，不安排实际发货。"
                     if ticket.get("reference_code") else "工单显示已完成，但暂未查到受理凭证。请先刷新工单或联系管理员核实，我目前无法确认受理结果。")
    return f"订单 **{ticket['order_id']}** 的换货进度是：**{status}**。\n\n{next_step}\n\n工单编号：{ticket['id']}。"


def scope_error(messages, name, arguments, actions=None):
    if actions is None:
        actions = requested_actions(current_text(messages))
    order_id = arguments.get("order_id")
    if order_id and grounded_id(messages, order_id, "ORD") is None:
        return "请明确本次要查询或办理的唯一订单号。"
    if name == "search_knowledge":
        if arguments.get("overview") and actions:
            return "办理申请需要针对性检索，请使用 overview=false。"
        order = identifiers(messages, "ORD") or context_data(messages).get("facts", {}).get("resolved_order_id")
        if order and not order_id:
            return "本次咨询包含订单号，请按该订单核实商品后检索。"
        code = arguments.get("model_code")
        catalog = context_data(messages).get("facts", {}).get("catalog_products", [])
        verified_code = any(p.get("model_code") == code for p in catalog)
        if code and not verified_code and grounded_id(messages, code, code.split("-")[0]) is None:
            return "请提供明确的商品型号，或使用订单号核实商品。"
        # 明确给了型号时，不允许模型省略范围而取通用资料冒充型号答案。
        explicit = re.findall(r"\b[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", current_text(messages))
        models = {value.casefold() for value in explicit if not value.upper().startswith(("ORD-", "TKT-"))}
        if code and models and code.casefold() not in models:
            return "请按客户本次指定的商品型号检索，不要替换成其他型号。"
        if explicit and not order_id and not code:
            return "请先核实消息中的订单或商品型号，再按对应范围检索。"
    return None


def compile_chat_graph(model, fallback=None):
    caller = ModelCalls(model, fallback)
    all_tools = {t.name: t for t in build_after_sales_tools()}

    def allowed(intent):
        actions = intent.actions
        writes = set()
        if "refund" in actions:
            writes.update(("issue_refund", "escalate_to_human"))
        if "exchange" in actions:
            writes.add("create_exchange_request")
        return {n: t for n, t in all_tools.items() if n not in WRITE_TOOLS or n in writes}

    async def agent(state: ChatState, config):
        messages = state["messages"]
        if "tool_count" not in state:
            # 只在首轮装配历史和摘要；后续保留完整的工具请求/响应配对。
            messages = assemble(messages, get_after_sales_prompt())
            intent = await classify_intent(messages, config)
            messages.insert(1, SystemMessage(
                f"本轮意图分类为 {intent.intent}。query 仅查询；refund/exchange 仅开放对应申请工具；"
                "clarify 时先询问客户希望查询、退款还是换货。仍需核对订单及业务条件。"))
        else:
            intent = TurnIntent.model_validate(state["turn_intent"])
        _ctx(config).turn_intent = intent
        raw = await caller.call(messages, config, node="agent", tools=list(allowed(intent).values()))
        return {"messages": Overwrite(messages + [raw]) if "tool_count" not in state else [raw],
                "tool_count": state.get("tool_count", 0), "turn_intent": intent.model_dump()}

    async def tools(state: ChatState, config):
        messages, count = [], state["tool_count"]
        updates = {}
        intent = TurnIntent.model_validate(state["turn_intent"])
        available = allowed(intent)
        for call in state["messages"][-1].tool_calls:
            name, args = call["name"], call.get("args", {})
            error = scope_error(state["messages"], name, args, intent.actions)
            if count >= get_settings().agent_max_tool_calls:
                result = failure("INVALID_ARGUMENT", "本轮已达到工具调用上限，请根据已有结果回复。")
                content = result.model_dump_json()
            elif name not in available or error:
                content = failure("INVALID_ARGUMENT", error or "当前请求不允许此操作。").model_dump_json()
                count += 1
            else:
                count += 1
                try:
                    content = await available[name].ainvoke(args, config=config)
                except (ValidationError, ValueError, TypeError):
                    content = failure("INVALID_ARGUMENT", "工具参数无效，请补充明确信息。").model_dump_json()
            data = json.loads(content)
            messages.append(ToolMessage(content=content, name=name, tool_call_id=call["id"]))
            if data.get("ok"):
                facts = data["data"]
                if name == "get_order":
                    updates["order_facts"] = facts
                elif name == "search_knowledge":
                    updates["knowledge_evidence"] = facts
                elif name == "create_exchange_request":
                    updates["ticket_result"] = facts
                    updates["workflow_result"] = _ctx(config).workflow_result
                elif name == "get_tickets" and len(facts.get("tickets", [])) == 1:
                    updates["ticket_result"] = facts["tickets"][0]
        return {"messages": messages, "tool_count": count, **updates}

    async def finalize(state: ChatState, config):
        last = state["messages"][-1]
        text = message_text(last) if isinstance(last, AIMessage) and not last.tool_calls else ""
        ticket = state.get("ticket_result")
        # 工单回复使用实际状态与凭证，避免模型把提交、批准和完成混为一谈。
        if ticket:
            text = ticket_reply(ticket)
        elif claims_exchange_success(text):
            text = "尚未取得换货办理记录，请先查询工单或明确申请换货。"
        if not text.strip() or (isinstance(last, AIMessage) and last.invalid_tool_calls):
            text = "本轮处理已停止，请依据上方工具结果核实状态；需要继续时请补充订单号和具体诉求。"
        evidence = state.get("knowledge_evidence", {})
        result_updates = {}
        completed_write = False
        for message in reversed(state["messages"]):
            if isinstance(message, ToolMessage) and message.name in WRITE_TOOLS:
                completed_write = completed_write or bool(json.loads(message.content).get("ok"))
            if isinstance(message, ToolMessage) and message.name == "escalate_to_human":
                result = json.loads(message.content)
                data = result.get("data") or {}
                if result.get("ok") and data.get("decision") == "escalated":
                    text = (f"订单 **{data['order_id']}** 的退款申请已转交人工审核，目前尚未批准退款。"
                            "请保留订单号，后续联系人工客服核实审核结果；本轮没有生成退款成功记录。")
        if evidence.get("status") == "unavailable" and not ticket and not completed_write:
            text = "这次知识检索服务暂时没有连通，我还没能读到对应资料，因此无法核实商品参数或排查建议。您提供的订单号可以保留，不用反复输入；稍后可以重试知识咨询，也可以继续查询订单或售后工单。"
        elif evidence.get("status") == "insufficient_evidence" and not ticket and not completed_write:
            question = current_text(state["messages"])[:160]
            text = f"关于您问的“{question}”，这次检索尚未找到直接依据，需要补充对应资料后再确认。您可以提供说明书中的相关文字，或联系人工客服核实这一项。"
            if TurnIntent.model_validate(state["turn_intent"]).actions:
                text += "目前缺少办理所需的有效资料，尚未据此提交新的换货申请。"
        elif evidence.get("sources") and not ticket and not completed_write:
            dialogue = [m for m in state["messages"] if isinstance(m, (HumanMessage, AIMessage))
                        and not getattr(m, "tool_calls", None) and "context_data" not in m.additional_kwargs]
            index = max(i for i, m in enumerate(dialogue) if isinstance(m, HumanMessage))
            history = [{"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": message_text(m)[-600:]}
                       for m in dialogue[:index][-4:]]
            answer = await ground_answer(current_text(state["messages"]), evidence, caller, config, history=history)
            text = answer.text
            result_updates["knowledge_evidence"] = {**evidence, "answer_status": answer.status, "sources": answer.sources}
        for message in reversed(state["messages"]):
            if not isinstance(message, ToolMessage):
                continue
            result = json.loads(message.content)
            if (result.get("error") or {}).get("code") == "OUTCOME_UNKNOWN":
                text = "暂未取得办理结果，请查询订单或工单确认状态。"
                break
        return {"final_response": text, **result_updates}

    graph = StateGraph(ChatState)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_node("finalize", finalize)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", lambda s: "tools" if s["messages"][-1].tool_calls else "finalize")
    graph.add_conditional_edges("tools", lambda s: "finalize" if s["tool_count"] >= get_settings().agent_max_tool_calls else "agent")
    graph.add_edge("finalize", END)
    return graph.compile()


def claims_exchange_success(text):
    # Query replies have no write result. Negated/conditional wording is allowed.
    for sentence in re.split(r"[。！？!?；;，,\n]", text):
        sentence = re.sub(r"[*_`]", "", sentence)
        if re.search(
            r"未|没有|不代表|不能|不会|尚未|如果|若|审批后|\b(?:not|cannot|if|pending)\b",
            sentence,
            re.IGNORECASE,
        ):
            continue
        for match in re.finditer(
            r"(?:换货|申请|工单).{0,16}(?:已提交|提交成功|已批准|已获批准|审批通过|批准成功|已受理|受理成功|已完成|完成换货)|(?:已|成功).{0,6}(?:为您|为你)?(?:提交|批准|受理|完成).{0,8}(?:换货|申请|工单)|\bexchange\b.{0,32}\b(?:approved|accepted|completed|submitted)\b",
            sentence,
            re.IGNORECASE,
        ):
            prefix, suffix = sentence[:match.start()], sentence[match.end():]
            if (re.search(r"(?:才能|才可|才算|只有|直到|一旦|假如|假设).{0,24}$", prefix)
                    or re.match(r"(?:之前|之后|以前|以后|前|后|时|才能|才会)", suffix)):
                continue
            return True
    return False
