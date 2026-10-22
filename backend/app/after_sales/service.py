"""换货领域事务。T1只供服务调用，公开写接口和工作流协调留到T5。"""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.after_sales.locking import begin_order_write, lock_order
from app.after_sales.schemas import (
    AdminTicketDetail,
    ApprovalOut,
    DecisionInput,
    ExchangeInput,
    RequestInput,
    SupplementInput,
    TicketDetail,
    TicketEventOut,
    TicketOrderOut,
    TicketSummary,
)
from app.auth import Principal
from app.config import get_settings
from app.db import session as db
from app.db.models import (
    ACTIVE_TICKET_STATUSES,
    AfterSalesTicket,
    Conversation,
    ExchangeAcceptance,
    Order,
    TicketApproval,
    TicketEvent,
    TicketRequest,
    utcnow,
)
from app.policy.exchange import evaluate_exchange


class TicketError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _not_found():
    return TicketError(404, "NOT_FOUND_OR_NOT_OWNED", "未找到该订单或工单。")


def _conflict(message="当前工单状态不允许此操作。", code="INVALID_TRANSITION"):
    return TicketError(409, code, message)


def _require_role(principal: Principal, role: str | None = None):
    if principal.role not in ("customer", "admin") or (role and principal.role != role):
        raise TicketError(403, "FORBIDDEN", "当前身份无权执行此操作。")


def _visible(principal: Principal, ticket: AfterSalesTicket):
    owner = principal.scope if principal.is_admin else principal.sub
    return owner is None or ticket.customer_id == owner


def _event(session, ticket, kind, actor, payload=None):
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            kind=kind,
            actor_type=actor.role if actor else "system",
            actor_id=actor.sub if actor else "exchange",
            payload_json=payload or {"status": ticket.status},
        )
    )


def _transition(ticket, status, code=None):
    ticket.status = status
    ticket.version += 1
    ticket.updated_at = utcnow()
    ticket.last_error_code = code
    ticket.last_error_message = None


def _policy(order, ticket):
    return evaluate_exchange(
        order,
        customer_id=ticket.customer_id,
        submitted_at=ticket.created_at,
        window_days=get_settings().exchange_window_days,
        reason_type=ticket.reason_type,
        description=ticket.description,
        requested_change=ticket.requested_change,
    )


class TicketService:
    async def _existing(self, session, order_id):
        acceptance = await session.scalar(
            select(ExchangeAcceptance).where(ExchangeAcceptance.order_id == order_id)
        )
        if acceptance:
            return await session.get(AfterSalesTicket, acceptance.ticket_id)
        active = await session.scalar(
            select(AfterSalesTicket).where(
                AfterSalesTicket.order_id == order_id,
                AfterSalesTicket.type == "exchange",
                AfterSalesTicket.status.in_(ACTIVE_TICKET_STATUSES),
            )
        )
        if active:
            return active
        failed = await session.scalar(
            select(AfterSalesTicket.id).where(
                AfterSalesTicket.order_id == order_id, AfterSalesTicket.status == "FAILED_TERMINAL"
            )
        )
        if failed:
            raise _conflict("该订单存在需要人工处置的工单。", "MANUAL_REQUIRED")
        return None

    async def create_or_find_exchange(
        self,
        principal: Principal,
        order_id: str,
        data: ExchangeInput,
        *,
        origin_conversation_id: str | None = None,
    ) -> AfterSalesTicket:
        _require_role(principal, "customer")
        try:
            async with db.SessionLocal() as session, session.begin():
                await begin_order_write(session)
                order = await lock_order(session, order_id)
                if order is None or order.customer_id != principal.sub:
                    raise _not_found()
                if origin_conversation_id:
                    conversation = await session.get(Conversation, origin_conversation_id)
                    if conversation is None or conversation.customer_id != principal.sub:
                        raise _not_found()
                # 先查既有业务事实；审批等待期间不以新一轮聊天时间重新判断申请期限。
                existing = await self._existing(session, order_id)
                if existing:
                    return existing
                submitted_at = utcnow()
                result = evaluate_exchange(
                    order,
                    customer_id=principal.sub,
                    submitted_at=submitted_at,
                    window_days=get_settings().exchange_window_days,
                    reason_type=data.reason_type,
                    description=data.description,
                    requested_change=data.requested_change,
                )
                ticket_id = "TKT-" + uuid4().hex
                ticket = AfterSalesTicket(
                    id=ticket_id,
                    customer_id=principal.sub,
                    order_id=order_id,
                    origin_conversation_id=origin_conversation_id,
                    workflow_thread_id="exchange:" + ticket_id,
                    description=data.description,
                    reason_type=data.reason_type,
                    requested_change=data.requested_change,
                    evidence_refs=[ref.model_dump() for ref in data.evidence_refs],
                    status=result.status,
                    created_at=submitted_at,
                    updated_at=submitted_at,
                    last_error_code=result.code,
                )
                session.add(ticket)
                await session.flush()
                _event(
                    session,
                    ticket,
                    "CREATED",
                    principal,
                    {
                        "status": ticket.status,
                        "code": result.code,
                        "missing_fields": result.missing_fields,
                    },
                )
                return ticket
        except IntegrityError:
            # 部分唯一索引为跨会话最终防线；只返回授权订单上实际胜出的既有事实。
            async with db.SessionLocal() as session:
                existing = await self._existing(session, order_id)
                if existing and existing.customer_id == principal.sub:
                    return existing
            raise

    async def _lock_ticket(self, session, ticket_id, principal=None):
        await begin_order_write(session)
        order_id = await session.scalar(
            select(AfterSalesTicket.order_id).where(AfterSalesTicket.id == ticket_id)
        )
        if order_id is None:
            raise _not_found()
        order = await lock_order(session, order_id)
        ticket = await session.scalar(
            select(AfterSalesTicket).where(AfterSalesTicket.id == ticket_id).with_for_update()
        )
        if ticket is None or order is None or (principal and not _visible(principal, ticket)):
            raise _not_found()
        return order, ticket

    async def supplement(self, principal: Principal, ticket_id: str, data: SupplementInput) -> dict:
        return await self._request(principal, ticket_id, "supplement", data)

    async def cancel(self, principal: Principal, ticket_id: str, data: RequestInput) -> dict:
        return await self._request(principal, ticket_id, "cancel", data)

    async def decide(self, principal: Principal, ticket_id: str, data: DecisionInput) -> dict:
        return await self._request(principal, ticket_id, "decision", data)

    async def _request(self, principal, ticket_id, operation, data):
        _require_role(principal, "admin" if operation == "decision" else "customer")
        payload = data.model_dump(exclude={"request_id"})
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        async with db.SessionLocal() as session, session.begin():
            order, ticket = await self._lock_ticket(session, ticket_id, principal)
            recorded = await session.scalar(
                select(TicketRequest).where(
                    TicketRequest.ticket_id == ticket_id,
                    TicketRequest.operation == operation,
                    TicketRequest.actor_type == principal.role,
                    TicketRequest.actor_subject == principal.sub,
                    TicketRequest.request_id == data.request_id,
                )
            )
            # 鉴权在前，重放在状态约束之前；材料已提交后即使状态改变也返回原接收快照。
            if recorded:
                if recorded.payload_hash != digest:
                    raise _conflict("同一请求标识不能用于不同内容。", "EXECUTION_CONFLICT")
                return recorded.response_json
            if operation == "supplement":
                if ticket.status != "WAITING_INFO":
                    raise _conflict()
                ticket.description = data.description
                if data.reason_type is not None:
                    ticket.reason_type = data.reason_type
                if data.requested_change is not None:
                    ticket.requested_change = data.requested_change
                result = _policy(order, ticket)
                _transition(ticket, result.status, result.code)
                _event(session, ticket, "SUPPLEMENTED", principal)
            elif operation == "cancel":
                if ticket.status != "CANCELLED":
                    if ticket.status not in ("WAITING_INFO", "PENDING_REVIEW"):
                        raise _conflict()
                    _transition(ticket, "CANCELLED")
                    _event(session, ticket, "CANCELLED", principal)
            else:
                approval = await session.scalar(
                    select(TicketApproval).where(TicketApproval.ticket_id == ticket_id)
                )
                if approval:
                    if approval.decision != data.decision:
                        raise _conflict("该工单已有不同的最终审批结果。")
                else:
                    if ticket.status != "PENDING_REVIEW":
                        raise _conflict()
                    result = _policy(order, ticket)
                    if data.decision == "approved" and result.status == "WAITING_INFO":
                        raise _conflict("申请信息不足，不能批准。")
                    status = (
                        "APPROVED"
                        if data.decision == "approved" and result.status != "REJECTED"
                        else "REJECTED"
                    )
                    _transition(
                        ticket, status, result.code if result.status == "REJECTED" else None
                    )
                    session.add(
                        TicketApproval(
                            ticket_id=ticket.id,
                            admin_subject=principal.sub,
                            decision=data.decision,
                            comment=data.comment,
                        )
                    )
                    _event(
                        session,
                        ticket,
                        "DECIDED",
                        principal,
                        {"decision": data.decision, "status": status},
                    )
            snapshot = {
                "ticket_id": ticket.id,
                "status": ticket.status,
                "request_id": data.request_id,
            }
            session.add(
                TicketRequest(
                    ticket_id=ticket.id,
                    operation=operation,
                    actor_type=principal.role,
                    actor_subject=principal.sub,
                    request_id=data.request_id,
                    payload_hash=digest,
                    response_status=202,
                    response_json=snapshot,
                )
            )
            return snapshot

    async def accept_exchange(self, ticket_id: str) -> ExchangeAcceptance | None:
        """内部确定性执行入口：必须读取已保存审批；没有外部发货或支付调用。"""
        async with db.SessionLocal() as session, session.begin():
            order, ticket = await self._lock_ticket(session, ticket_id)
            existing = await session.scalar(
                select(ExchangeAcceptance).where(ExchangeAcceptance.ticket_id == ticket_id)
            )
            if existing:
                return existing
            approval = await session.scalar(
                select(TicketApproval).where(TicketApproval.ticket_id == ticket_id)
            )
            if (
                ticket.status not in ("APPROVED", "FAILED_RETRYABLE")
                or approval is None
                or approval.decision != "approved"
            ):
                raise _conflict("工单尚未通过审批或已经结束。")
            result = _policy(order, ticket)
            if result.status != "PENDING_REVIEW":
                _transition(ticket, "REJECTED", result.code or "INVALID_INPUT")
                _event(
                    session,
                    ticket,
                    "POLICY_REJECTED",
                    None,
                    {"status": ticket.status, "code": ticket.last_error_code},
                )
                return None
            acceptance = ExchangeAcceptance(
                ticket_id=ticket.id,
                order_id=order.id,
                idempotency_key="exchange_acceptance:" + ticket.id,
                reference_code="EXC-" + uuid4().hex[:20],
            )
            session.add(acceptance)
            _transition(ticket, "COMPLETED")
            _event(
                session,
                ticket,
                "COMPLETED",
                None,
                {"status": "COMPLETED", "reference_code": acceptance.reference_code},
            )
            await session.flush()
            return acceptance

    async def list_tickets(self, principal: Principal, *, status=None, limit=50, offset=0):
        _require_role(principal)
        owner = principal.scope if principal.is_admin else principal.sub
        stmt = select(AfterSalesTicket)
        if owner is not None:
            stmt = stmt.where(AfterSalesTicket.customer_id == owner)
        if status is not None:
            stmt = stmt.where(AfterSalesTicket.status == status)
        async with db.SessionLocal() as session:
            rows = await session.scalars(
                stmt.order_by(AfterSalesTicket.created_at.desc(), AfterSalesTicket.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [TicketSummary.model_validate(row) for row in rows]

    async def detail(self, principal: Principal, ticket_id: str):
        _require_role(principal)
        async with db.SessionLocal() as session:
            ticket = await session.get(AfterSalesTicket, ticket_id)
            if ticket is None or not _visible(principal, ticket):
                raise _not_found()
            acceptance = await session.scalar(
                select(ExchangeAcceptance).where(ExchangeAcceptance.ticket_id == ticket_id)
            )
            events = list(
                await session.scalars(
                    select(TicketEvent)
                    .where(TicketEvent.ticket_id == ticket_id)
                    .order_by(TicketEvent.id.desc())
                    .limit(200)
                )
            )
            fields = TicketSummary.model_validate(ticket).model_dump() | {
                "reason_type": ticket.reason_type,
                "requested_change": ticket.requested_change,
                "evidence_refs": ticket.evidence_refs,
                "reference_code": acceptance.reference_code if acceptance else None,
                "events": [TicketEventOut.model_validate(event) for event in reversed(events)],
            }
            if principal.is_admin:
                order = await session.get(Order, ticket.order_id)
                approval = await session.scalar(
                    select(TicketApproval).where(TicketApproval.ticket_id == ticket_id)
                )
                return AdminTicketDetail(
                    **fields,
                    customer_id=ticket.customer_id,
                    order=TicketOrderOut.model_validate(order),
                    approval=ApprovalOut.model_validate(approval) if approval else None,
                    last_error_message=ticket.last_error_message,
                )
            return TicketDetail(**fields)
