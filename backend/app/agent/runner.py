"""驱动智能体完成一轮对话，并发送执行事件。

`run_agent_turn` 是异步生成器：它针对当前会话运行 LangGraph 智能体，
为模型输出、工具调用、工具结果和决策等步骤保存 ReasoningEvent，
通过 Redis 发布/订阅广播给管理面板，并逐步返回给调用方的聊天 SSE 流。
具体退款规则由工具内的确定性规则引擎强制执行，此层负责观察和记录。

客户 ID 来自已验证的 JWT（见 `app/auth.py`），接口也已核对会话归属；
这里会再次检查身份和归属，形成纵深防护。

每次写入单独打开数据库会话，等待模型响应期间不持有连接，避免长连接事件流
在空闲期间耗尽 Pod 的数据库连接池。

本层执行的运行边界与结果核对：
- 每个会话的**词元预算**（`max_conversation_tokens`）和每位客户的
  **每日预算**（`customer_daily_token_budget`）：超限后，下一轮直接礼貌拒绝，
  不再调用模型。公开演示模式（`AUTH_MODE=demo`）则改用脚本模型；
  整个演示的每日预算耗尽时也采用同样方式。
- LangGraph 的**递归上限**（`agent_recursion_limit`）限制工具调用循环次数，
  `turn_timeout_s` 限制整轮实际耗时。
- **输出纠正**：如果最终回复声称退款已批准或已处理，但本轮没有任何
  `issue_refund` 调用返回 `approved`，则在回复前添加明确的纠正说明，
  并发送 `output_correction` 事件。工具内的规则校验保护退款记录，
  此处进一步约束聊天回复中的成功声明。

配置 Opik 后（`app/tracing.py`），本轮也会生成 Opik 追踪记录，
并通过末尾的 `trace` 步骤将执行时间线关联到该记录。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import insert, select, update

from app import redis as shared
from app import tracing
from app.agent.graph import get_agent
from app.agent.llm import primary_model_name, use_prompt_caching
from app.agent.prompts import get_system_prompt
from app.agent.tools import ToolContext, tool_config
from app.config import get_settings
from app.db import session as db
from app.db.models import Conversation, Message, ReasoningEvent, utcnow
from app.events import broadcaster
from app.observability import (
    LLM_FALLBACKS,
    LLM_TOKENS,
    REFUND_DECISIONS,
    TOOL_CALLS,
    TURN_LATENCY,
    TURNS,
    TURNS_IN_FLIGHT,
    conversation_id_var,
    log_event,
)

logger = logging.getLogger(__name__)

# 限制带入模型的历史消息数量，防止上下文无限增长。
_HISTORY_LIMIT = 40

_ERROR_MESSAGE = "抱歉，处理请求时服务出现异常，请稍后重试。"

_DECISION_TOOLS = {"issue_refund", "escalate_to_human"}

# 匹配肯定宣称退款已获批的语句。如果最终回复包含此类表述，
# 但本轮没有 `issue_refund` 返回 "approved"，则在回复前添加纠正说明。
#
# 应匹配完整的肯定成功表达，避免仅凭 "approved" 或 "refunded" 等单词触发，
# 从而减少对“该订单此前已经退款”或“无法自动批准”等合理说明的误报。
# 中间词语结构也会自然排除 "has not been approved" 等否定形式，
# 因为否定词打断了 "has been approved" 的连续结构。
_APPROVAL_CLAIM = re.compile(
    r"(?:"
    # 匹配“您的/这笔退款［金额为 $X］已批准/处理/发放”等英文表达。
    r"(?:your |the )?refund(?:\s+of\s+\$[\d,.]+)?\s+(?:has been|is|was)\s+"
    r"(?:approved|processed|issued|completed|refunded)"
    # 匹配“成功/现已批准、处理、发放或退款”等英文表达。
    r"|(?:successfully|now)\s+(?:approved|processed|issued|refunded)"
    # 匹配“我已批准/处理/发放您的退款”等英文表达。
    r"|i(?:'ve| have)\s+(?:approved|processed|issued)\s+(?:your |the )?refund"
    # 匹配“已批准/处理/发放您的退款”等英文表达。
    r"|(?:approved|processed|issued)\s+your\s+refund"
    r")",
    re.I,
)

# 前面带有否定词的语句，如“没有发放退款”“一笔退款也未处理”，属于否定说明。
_NEGATED = re.compile(r"\b(?:no|not|never|without|nor)\b[\s\w,'-]{0,12}$", re.I)
# 智能体明确拒绝作出的声明也应排除，例如对抗测试发现的“我无法绕过限额，
# 也无法确认退款已发放”。仅排除拒绝动词直接约束的声明；
# “尽管我通常不能这样做，您的退款已获批准”仍属于成功声明。
_DECLINED = re.compile(
    r"\b(?:can[’']?t|cannot|can not|won[’']t|will not|unable to|not able to)\b"
    r"[^.!?\n]{0,60}?\b(?:confirm|say|state|claim|pretend|report|promise|guarantee|tell you|include|write)\s+"
    # 允许 "that the $89 "、"that another **$59** "、"“Your second " 等前缀，
    # 最多三个短词，并兼容 Markdown 强调标记。
    r"(?:that\s+)?[“\"']?(?:[\w$,.*]+\s+){0,3}$",
    re.I,
)


# 中文成功声明与否定/条件/历史说明分开匹配，不能用“包含退款”代替语义判断。
_ZH_APPROVAL_CLAIM = re.compile(
    r"退款(?:申请)?(?:已(?:经)?|现已)(?:获(?:得)?)?(?:成功)?(?:批准|通过|处理(?:完成|成功)?|完成|到账|退回)"
    r"|退款(?:处理|办理|审核)?(?:成功|通过)"
    r"|(?:已(?:经)?|现已)(?:成功)?(?:为[您你])?(?:成功)?(?:办理|处理|完成|批准|发放)?"
    r"了?(?:您的?|你的?|该笔|这笔)?退款"
)
_ZH_NONASSERTIVE_PREFIX = re.compile(
    r"(?:(?:不能|无法|不会|尚未|并未|没有|不应|未能|未曾)"
    r"(?:直接|自动|再次|立即|为您|为你|向您|向你|您的|你的|保证|确认|声称|表示|宣称|承诺|说|告知|认定|已|[：:‘’“”\" ])*"
    r"|(?:如果|假如|假设|若|当|一旦)[^，。！？；\n]{0,24}"
    # 只有明确的历史词才在这里排除；“订单＋订单号”本身不能证明是在介绍过去。
    r"|(?:此前|之前|先前|过去|曾经|历史上))$"
)
# “订单已退款，不能重复办理”是在说明已有状态，需要同时匹配后面的拒绝重复语境。
_ZH_HISTORICAL_ORDER_PREFIX = re.compile(
    r"(?:该|这笔|此|这个)?订单(?:\s*[A-Za-z0-9-]+)?\s*$"
)
_ZH_REPEAT_REFUND_SUFFIX = re.compile(
    r"^[，,；;。\s]*(?:不能|无法|不可|不允许)(?:再次|重复)(?:办理|退款)"
)
_ZH_CONDITIONAL_SUFFIX = re.compile(r"^(?:后|之后|以后|时|的话|才能|才会)")


def claims_approval(text: str) -> bool:
    """识别中英文肯定的退款成功声明；保留英文规则并排除中文常见非肯定表达。"""
    if any(
        not _NEGATED.search(text[max(0, m.start() - 24) : m.start()])
        and not _DECLINED.search(text[max(0, m.start() - 100) : m.start()])
        for m in _APPROVAL_CLAIM.finditer(text)
    ):
        return True
    plain = re.sub(r"[*_`]", "", text)
    for match in _ZH_APPROVAL_CLAIM.finditer(plain):
        prefix = plain[max(0, match.start() - 50):match.start()]
        suffix = plain[match.end():]
        if _ZH_NONASSERTIVE_PREFIX.search(prefix) or _ZH_CONDITIONAL_SUFFIX.search(suffix):
            continue
        # 仅排除“订单已退款＋不能再退”这种状态说明；
        # “订单已为您办理退款”含有办理动作，即使后面提醒勿重复申请，也必须核对本轮结果。
        if (
            match.group() in {"已退款", "已经退款"}
            and _ZH_HISTORICAL_ORDER_PREFIX.search(prefix)
            and _ZH_REPEAT_REFUND_SUFFIX.search(suffix)
        ):
            continue
        return True
    return False


_BUDGET_MESSAGE = "当前会话或账户的用量已达上限，请尝试新建对话；如仍需帮助，请联系人工客服。"

_CORRECTION_PREFIX = (
    "_系统提示：本轮没有记录退款批准结果。下方关于退款成功的表述未经系统确认，"
    "请以实际退款记录为准。_\n\n"
)


class ConversationNotFound(Exception):
    pass


def new_conversation_id() -> str:
    return f"conv-{uuid.uuid4().hex}"


async def create_conversation(customer_id: str, cid: str | None = None) -> str:
    cid = cid or new_conversation_id()
    async with db.SessionLocal() as session:
        session.add(Conversation(id=cid, customer_id=customer_id))
        await session.commit()
    return cid


async def load_owned_conversation(customer_id: str, conversation_id: str) -> Conversation:
    """返回属于 `customer_id` 的会话，否则抛出异常。

    对调用方统一处理不存在的会话和他人会话，避免通过 ID 探测会话信息。
    """
    async with db.SessionLocal() as session:
        convo = await session.get(Conversation, conversation_id)
    if convo is None or convo.customer_id != customer_id:
        raise ConversationNotFound(conversation_id)
    return convo


async def _history(conversation_id: str) -> list:
    async with db.SessionLocal() as session:
        rows = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.desc())
                .limit(_HISTORY_LIMIT)
            )
        ).all()
    # 查询先取最新的有限条消息，再反转为时间正序，作为本轮模型的对话上下文。
    return [
        HumanMessage(m.content) if m.role == "user" else AIMessage(m.content)
        for m in reversed(rows)
    ]


def _system_message() -> SystemMessage:
    prompt = get_system_prompt()
    if use_prompt_caching():
        # 仅在供应商支持时标记稳定的提示词前缀供其缓存；这与数据库保存聊天记录不同。
        return SystemMessage(
            content=[{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]
        )
    return SystemMessage(prompt)


def _step_type_for_tool(name: str) -> str:
    if name == "check_refund_eligibility":
        return "policy_eval"
    if name in _DECISION_TOOLS:
        return "decision"
    return "tool_result"


async def _save_message(cid: str, role: str, content: str) -> int:
    async with db.SessionLocal() as session:
        msg = Message(conversation_id=cid, role=role, content=content)
        session.add(msg)
        await session.commit()
        return msg.id


async def run_agent_turn(
    customer_id: str, conversation_id: str | None, user_text: str
) -> AsyncIterator[dict]:
    settings = get_settings()
    started = time.perf_counter()
    if conversation_id is None:
        conversation_id = await create_conversation(customer_id)
    convo = await load_owned_conversation(customer_id, conversation_id)
    cid = convo.id
    conversation_id_var.set(cid)

    # 先将用户消息写入数据库，后续加载历史时会读到它。
    user_msg_id = await _save_message(cid, "user", user_text)
    # 管理端通过广播收到这条用户消息；客户页面已先显示自己的输入，不再重复回传。
    await broadcaster.publish(cid, {"kind": "message", "role": "user", "content": user_text})

    async def emit(step_type: str, node: str, payload: dict, tokens: int = 0) -> dict:
        # 每个执行步骤都在短事务中写入数据库，随后广播给管理端，并返回给客户 SSE 流。
        async with db.SessionLocal() as session:
            # 在数据库中原子递增会话序号，保证多个后端实例产生的事件有统一顺序。
            values = {"event_seq": Conversation.event_seq + 1}
            if tokens:
                values["tokens_used"] = Conversation.tokens_used + tokens
            seq = await session.scalar(
                update(Conversation)
                .where(Conversation.id == cid)
                .values(**values)
                .returning(Conversation.event_seq)
            )
            created = utcnow()
            row_id = await session.scalar(
                insert(ReasoningEvent)
                .values(
                    conversation_id=cid,
                    message_id=user_msg_id,
                    seq=seq,
                    step_type=step_type,
                    node=node,
                    payload=payload,
                    created_at=created,
                )
                .returning(ReasoningEvent.id)
            )
            await session.commit()
        event = {
            "kind": "step",
            "id": row_id,
            "seq": seq,
            "step_type": step_type,
            "node": node,
            "payload": payload,
            "created_at": created.isoformat(),
        }
        await broadcaster.publish(cid, event)
        return event

    # 最终回复单独保存为聊天消息；右侧的工具调用等执行事件另存于 ReasoningEvent。
    async def finish(final_text: str, outcome: str) -> list[dict]:
        await _save_message(cid, "assistant", final_text)
        final_event = {"kind": "message", "role": "assistant", "content": final_text}
        await broadcaster.publish(cid, final_event)
        TURNS.labels(outcome).inc()
        TURN_LATENCY.observe(time.perf_counter() - started)
        return [final_event, {"kind": "done"}]

    yield {"kind": "conversation", "conversation_id": cid}

    # 在调用模型前检查用量；公开演示超额后降级到脚本模型，继续使用同一套工具。
    prior_tokens = convo.tokens_used or 0
    today_tokens = await shared.customer_tokens_today(customer_id)
    over_budget = (
        prior_tokens >= settings.max_conversation_tokens
        or today_tokens >= settings.customer_daily_token_budget
    )
    scripted = False
    if settings.is_demo:
        # 公开演示在用户或全局额度耗尽后改用脚本模型，避免继续消耗真实模型额度。
        global_cap = settings.demo_global_daily_token_budget
        if over_budget or (global_cap and await shared.global_tokens_today() >= global_cap):
            scripted = True
            yield await emit(
                "notice",
                "guard",
                {
                    "reason": "demo_budget",
                    "message": "演示的大模型额度已用完，当前回复由脚本模型生成，退款规则与安全校验继续生效。",
                },
            )
    elif over_budget:
        yield await emit(
            "budget_exhausted",
            "guard",
            {
                "prior_tokens": prior_tokens,
                "limit": settings.max_conversation_tokens,
                "customer_tokens_today": today_tokens,
                "customer_daily_limit": settings.customer_daily_token_budget,
            },
        )
        for event in await finish(_BUDGET_MESSAGE, "budget_exhausted"):
            yield event
        return

    # 已验证的身份通过执行配置传给工具，不能由模型在工具参数中随意指定客户。
    ctx = ToolContext(conversation_id=cid, verified_customer_id=customer_id)
    agent = get_agent(scripted=scripted)
    tracer = tracing.tracer_for_turn(cid, customer_id, scripted=scripted)
    run_config = {
        "recursion_limit": settings.agent_recursion_limit,
        "run_name": "refund-turn",
        **tool_config(ctx),
    }
    if tracer is not None:
        run_config["callbacks"] = [tracer]
    primary_model = primary_model_name()

    # 当前用户消息已写入历史，这里不重复拼接。
    messages = [_system_message()] + await _history(cid)

    final_text = ""
    approved_in_turn = False
    decision_in_turn: str | None = None
    fallback_in_turn = False
    TURNS_IN_FLIGHT.inc()
    try:
        # 总超时限制整轮问答；recursion_limit 另行限制图的执行步数，防止工具循环不结束。
        async with asyncio.timeout(settings.turn_timeout_s):
            # updates 按节点输出消息更新，不是逐个字的流；一次问答可能经过多个模型/工具节点。
            async for chunk in agent.astream(
                {"messages": messages},
                stream_mode="updates",
                config=run_config,
            ):
                for node, update_ in chunk.items():
                    for msg in update_.get("messages", []):
                        if isinstance(msg, AIMessage):
                            usage = getattr(msg, "usage_metadata", None)
                            meta = getattr(msg, "response_metadata", None) or {}
                            model = meta.get("model") or meta.get("model_name") or ""
                            if model and not scripted and not model.startswith(primary_model):
                                # 返回的模型标识与主模型不同时，记录本轮使用过备用模型。
                                fallback_in_turn = True
                                LLM_FALLBACKS.labels(model).inc()
                            if isinstance(msg.content, str) and msg.content.strip():
                                yield await emit("model", node, {"text": msg.content})
                                final_text = msg.content
                            elif isinstance(msg.content, list):
                                text = "".join(
                                    b.get("text", "")
                                    for b in msg.content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                                if text.strip():
                                    yield await emit("model", node, {"text": text})
                                    final_text = text
                            # 此处记录并展示调用请求；实际执行发生在 graph.py 的 tools 节点。
                            for call in msg.tool_calls or []:
                                TOOL_CALLS.labels(call["name"]).inc()
                                yield await emit(
                                    "tool_call",
                                    node,
                                    {"tool": call["name"], "args": call.get("args", {})},
                                )
                            # 每次模型调用分别累计输入/输出词元，用于页面展示和后续额度检查。
                            if usage and not scripted:
                                tin = int(usage.get("input_tokens", 0))
                                tout = int(usage.get("output_tokens", 0))
                                LLM_TOKENS.labels("input").inc(tin)
                                LLM_TOKENS.labels("output").inc(tout)
                                await shared.add_customer_tokens(customer_id, tin + tout)
                                yield await emit(
                                    "usage",
                                    node,
                                    {"input_tokens": tin, "output_tokens": tout, "model": model},
                                    tokens=tin + tout,
                                )
                        elif isinstance(msg, ToolMessage):
                            try:
                                result = json.loads(msg.content)
                            except (json.JSONDecodeError, TypeError):
                                result = {"raw": str(msg.content)}
                            # 只有实际退款工具返回 approved 才算成功；不能把资格检查当作退款。
                            if msg.name in _DECISION_TOOLS and isinstance(result, dict):
                                decision = result.get("decision")
                                if decision:
                                    REFUND_DECISIONS.labels(decision).inc()
                                    decision_in_turn = decision
                                if msg.name == "issue_refund" and decision == "approved":
                                    approved_in_turn = True
                            yield await emit(
                                _step_type_for_tool(msg.name),
                                node,
                                {"tool": msg.name, "result": result},
                            )
    except Exception as exc:  # 处理模型或供应商错误、超时及递归次数超限。
        # 详细异常留在后端日志，前端只收到通用提示，避免暴露服务配置或内部细节。
        log_event(
            logger,
            "agent turn failed",
            level=logging.ERROR,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        TURNS.labels("error").inc()
        TURN_LATENCY.observe(time.perf_counter() - started)
        yield await emit("error", "agent", {"error_type": type(exc).__name__})
        trace = tracing.finish_turn(
            tracer,
            tracing.TurnOutcome(
                decision=decision_in_turn,
                fallback=fallback_in_turn,
                error=True,
            ),
        )
        if trace:
            yield await emit("trace", "opik", trace)
        yield {"kind": "error", "message": _ERROR_MESSAGE}
        return
    finally:
        TURNS_IN_FLIGHT.dec()

    if not final_text:
        final_text = "抱歉，暂时无法完成该请求，请稍后重试或联系人工客服。"

    # 核对最终文案与本轮工具结果；虚假的成功声明需要在客户回复中明确纠正。
    # 这里的文本检测只负责补充提醒，实际能否退款仍由 tools.py 和规则引擎决定。
    corrected = not approved_in_turn and claims_approval(final_text)
    if corrected:
        yield await emit(
            "output_correction",
            "sanitizer",
            {
                "reason": "回复声称退款成功，但本轮退款工具没有返回批准结果。",
                "original": final_text,
            },
        )
        final_text = _CORRECTION_PREFIX + final_text

    trace = tracing.finish_turn(
        tracer,
        tracing.TurnOutcome(
            decision=decision_in_turn,
            corrected=corrected,
            fallback=fallback_in_turn,
        ),
    )
    if trace:
        yield await emit("trace", "opik", trace)

    for event in await finish(final_text, "ok"):
        yield event
