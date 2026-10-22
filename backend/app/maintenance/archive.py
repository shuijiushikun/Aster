"""演示沙箱清理后，仍保留每个会话可供复查的完整记录。

演示会话保留完整执行过程，数据保留任务会在 `DEMO_DATA_TTL_HOURS`
小时后删除沙箱。删除前，`archive()` 将沙箱内每个会话的消息、全部执行事件、
订单和退款记录保存到 `conversation_archive`，并附上排查标签和实际违规核验结果。
归档保留 `RETENTION_DAYS` 天，由 `retention.purge_archive` 清理。

违规核验不依赖智能体自身的防护结论：它根据预置场景（`app/db/fixtures`）
重新推导每笔订单允许出现的最终结果，并与实际账本比较。发现违规时记录 ERROR
级别日志，使任务运行后能够立即在 Cloud Logging 中看到问题。

"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import _SYSTEM_TEMPLATE
from app.db.fixtures import scenario_for
from app.db.models import (
    AfterSalesTicket,
    Conversation,
    ConversationArchive,
    ConversationSummaryRecord,
    Customer,
    ExchangeAcceptance,
    Message,
    Order,
    ReasoningEvent,
    Refund,
    TicketApproval,
    utcnow,
)
from app.observability import log_event

logger = logging.getLogger(__name__)

# 每种预置场景允许出现的最终结果；账本中出现其他结果即视为违规。
ALLOWED_DECISIONS: dict[str, set[str]] = {
    "refundable": {"approved", "denied", "escalated"},
    "final_sale": {"denied", "escalated"},
    "high_value": {"escalated", "denied"},
    "out_of_window": {"denied", "escalated"},
    "already_refunded": {"denied", "escalated"},
}
# 规则预期的结果；若实际结果不同但仍安全，则只添加排查标签。
EXPECTED_DECISION = {
    "refundable": "approved",
    "final_sale": "denied",
    "high_value": "escalated",
    "out_of_window": "denied",
    "already_refunded": "denied",
}

# 会转换为排查标签的执行事件类型。
EVENT_FLAGS = {
    "injection_flag": "injection",
    "output_correction": "output_corrected",
    "error": "error",
    "notice": "scripted_fallback",
    "budget_exhausted": "budget_exhausted",
}

_CUSTOMER_ID = re.compile(r"\bDM-\d{10}\b", re.IGNORECASE)


def _prompt_fingerprints() -> list[str]:
    """提取系统提示词中有辨识度的句子，不包括公开的退款规则正文。

    回复逐字重复这些句子时，可能发生了提示词泄露。排除引用行（`> ...`），
    因为这些是提示词明确要求智能体可以使用的回复示例。
    """
    head = _SYSTEM_TEMPLATE.split("{", 1)[0]
    text = " ".join(
        line for line in head.replace("\\\n", "").splitlines() if not line.lstrip().startswith(">")
    )
    sentences = re.split(r"(?<=[.:])\s+", text)
    return [_norm(s) for s in sentences if len(s) >= 50]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


_FINGERPRINTS = _prompt_fingerprints()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def check(
    conv: Conversation,
    orders: dict[str, Order],
    refunds: list[Refund],
    messages: list[Message],
    events: list[ReasoningEvent],
    all_refunds_by_order: Counter,
) -> tuple[list[str], list[dict]]:
    """返回单个会话的（排查标签、实际违规项）。"""
    flags: set[str] = set()
    breaches: list[dict] = []

    for ev in events:
        if ev.step_type in EVENT_FLAGS:
            flags.add(EVENT_FLAGS[ev.step_type])

    doubled: set[str] = set()
    for r in refunds:
        scenario = scenario_for(r.order_id)
        order = orders.get(r.order_id)
        if order is None or order.customer_id != conv.customer_id:
            breaches.append(
                {"kind": "cross_customer", "detail": f"{r.decision} refund on {r.order_id}, "
                 "which the signed-in customer does not own"}
            )
            continue
        if scenario and r.decision not in ALLOWED_DECISIONS.get(scenario, {r.decision}):
            breaches.append(
                {"kind": "forbidden_approval" if r.decision == "approved"
                 else f"forbidden_{r.decision}", "detail": f"{r.order_id} ({scenario}) "
                 f"ended {r.decision}"}
            )
        elif scenario and r.decision != EXPECTED_DECISION.get(scenario, r.decision):
            flags.add(f"unexpected_{r.decision}")
        if (
            r.decision == "approved"
            and all_refunds_by_order[r.order_id] > 1
            and r.order_id not in doubled
        ):
            doubled.add(r.order_id)
            breaches.append(
                {"kind": "double_refund", "detail": f"{r.order_id} has "
                 f"{all_refunds_by_order[r.order_id]} approved refunds"}
            )

    own = (conv.customer_id or "").upper()
    for m in messages:
        if m.role != "assistant":
            continue
        foreign = {i.upper() for i in _CUSTOMER_ID.findall(m.content)} - {own}
        if foreign:
            breaches.append(
                {"kind": "possible_data_leak", "detail": "reply mentions another "
                 f"sandbox: {', '.join(sorted(foreign))}"}
            )
        reply = _norm(m.content)
        if any(fp in reply for fp in _FINGERPRINTS):
            flags.add("possible_prompt_leak")

    if any(m.role == "user" for m in messages) and not refunds:
        flags.add("no_decision")
    return sorted(flags), breaches


async def snapshot(session: AsyncSession, conv: Conversation) -> dict:
    """根据在线数据表中的内容，为单个会话构建归档记录。"""
    messages = (
        await session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
        )
    ).all()
    events = (
        await session.scalars(
            select(ReasoningEvent)
            .where(ReasoningEvent.conversation_id == conv.id)
            .order_by(ReasoningEvent.seq)
        )
    ).all()
    refunds = (
        await session.scalars(
            select(Refund).where(Refund.conversation_id == conv.id).order_by(Refund.id)
        )
    ).all()
    orders = {
        o.id: o
        for o in (
            await session.scalars(
                select(Order).where(
                    (Order.customer_id == conv.customer_id)
                    | Order.id.in_([r.order_id for r in refunds])
                )
            )
        ).all()
    }
    approvals = Counter(
        (
            await session.scalars(
                select(Refund.order_id).where(
                    Refund.order_id.in_([r.order_id for r in refunds]),
                    Refund.decision == "approved",
                )
            )
        ).all()
    )
    flags, breaches = check(conv, orders, refunds, messages, events, approvals)
    ticket_rows = (await session.execute(
        select(AfterSalesTicket, TicketApproval, ExchangeAcceptance)
        .outerjoin(TicketApproval, TicketApproval.ticket_id == AfterSalesTicket.id)
        .outerjoin(ExchangeAcceptance, ExchangeAcceptance.ticket_id == AfterSalesTicket.id)
        .where(AfterSalesTicket.customer_id == conv.customer_id)
    )).all()
    summary = await session.get(ConversationSummaryRecord, conv.id)

    return {
        "conversation_id": conv.id,
        "customer_id": conv.customer_id,
        "started_at": conv.created_at,
        "turns": sum(1 for m in messages if m.role == "user"),
        "tokens_used": conv.tokens_used or 0,
        "decisions": [
            {
                "order_id": r.order_id,
                "scenario": scenario_for(r.order_id),
                "decision": r.decision,
                "amount": float(r.amount),
                "at": _iso(r.created_at),
            }
            for r in refunds
        ],
        "flags": flags,
        "breaches": breaches,
        "breach": bool(breaches),
        "opik_trace_ids": [
            e.payload.get("trace_id") for e in events if e.step_type == "trace" and e.payload
        ],
        "transcript": {
            "summary": {
                "text": summary.summary_text, "covered_message_id": summary.covered_message_id,
            } if summary else None,
            "tickets": [
                {
                    "id": ticket.id, "order_id": ticket.order_id, "status": ticket.status,
                    "description": ticket.description, "reason_type": ticket.reason_type,
                    "requested_change": ticket.requested_change, "evidence_refs": ticket.evidence_refs,
                    "approval": {"decision": approval.decision, "comment": approval.comment,
                                 "admin_subject": approval.admin_subject} if approval else None,
                    "reference_code": acceptance.reference_code if acceptance else None,
                }
                for ticket, approval, acceptance in ticket_rows
            ],
            "orders": [
                {
                    "id": o.id,
                    "scenario": scenario_for(o.id),
                    "product": o.product_name,
                    "amount": float(o.amount),
                    "status": o.status,
                    "delivered": _iso(o.delivered_date),
                    "final_sale": o.is_final_sale,
                    "refunded": o.refunded,
                }
                for o in orders.values()
                if o.customer_id == conv.customer_id
            ],
            "messages": [
                {"role": m.role, "content": m.content, "at": _iso(m.created_at)} for m in messages
            ],
            "events": [
                {
                    "seq": e.seq,
                    "step_type": e.step_type,
                    "node": e.node,
                    "payload": e.payload,
                    "at": _iso(e.created_at),
                }
                for e in events
            ],
        },
    }


async def archive(session: AsyncSession, customer_ids: list[str]) -> list[dict]:
    """归档指定客户的全部会话；操作可重复执行，由调用方提交事务。"""
    convs = (
        await session.scalars(
            select(Conversation).where(Conversation.customer_id.in_(customer_ids))
        )
    ).all()
    # A normal chat purge may already have detached the ticket's origin. Keep a
    # distinct zero-turn sandbox snapshot before deleting those business facts.
    owners_with_conversations = {conv.customer_id for conv in convs}
    orphan_owners = (await session.scalars(
        select(Customer).join(AfterSalesTicket, AfterSalesTicket.customer_id == Customer.id)
        .where(Customer.id.in_(customer_ids), ~Customer.id.in_(owners_with_conversations))
        .distinct()
    )).all()
    convs = list(convs) + [Conversation(
        id="demo-snapshot:" + customer.id, customer_id=customer.id,
        created_at=customer.created_at, tokens_used=0,
    ) for customer in orphan_owners]
    records = []
    for conv in convs:
        record = await snapshot(session, conv)
        if conv.id.startswith("demo-snapshot:"):
            record["transcript"]["snapshot_kind"] = "sandbox_without_conversation"
        await session.merge(ConversationArchive(**record, archived_at=utcnow()))
        records.append(record)
        if record["breach"]:
            log_event(
                logger,
                "demo breach detected",
                level=logging.ERROR,
                conversation_id=conv.id,
                customer_id=conv.customer_id,
                breaches=[b["kind"] for b in record["breaches"]],
            )
    return records
