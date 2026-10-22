from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Money = Numeric(10, 2, asdecimal=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def TZDateTime() -> DateTime:  # noqa: N802 - 此处按类型的使用习惯命名。
    return DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    loyalty_tier: Mapped[str] = mapped_column(String, default="standard")
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, default="general")
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[str] = mapped_column(String, default="delivered")
    order_date: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    delivered_date: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    is_final_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    refunded: Mapped[bool] = mapped_column(Boolean, default=False)

    customer: Mapped["Customer"] = relationship(back_populates="orders")


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        # 并发重复退款的最终防线：每笔订单最多只能存在一条获批退款记录。
        Index(
            "uq_refunds_one_approved_per_order",
            "order_id",
            unique=True,
            postgresql_where=text("decision = 'approved'"),
            sqlite_where=text("decision = 'approved'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)  # approved：批准；denied：拒绝；escalated：转人工。
    reason: Mapped[str] = mapped_column(String, default="")
    decided_by: Mapped[str] = mapped_column(String, default="agent")  # agent：智能体；human：人工。
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_customer_created", "customer_id", "created_at"),
        Index("ix_conversations_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True
    )
    # 每个会话内单调递增的事件计数器，通过原子操作递增。
    event_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 累计模型词元用量，用于检查每个会话的预算。
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String, nullable=False)  # user：用户；assistant：助手。
    content: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class ReasoningEvent(Base):
    __tablename__ = "reasoning_events"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_reasoning_events_conv_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # 事件类型：tool_call（工具调用）、tool_result（工具结果）、policy_eval（规则校验）、
    # decision（决策）、model（模型）、usage（用量）等。
    step_type: Mapped[str] = mapped_column(String, nullable=False)
    node: Mapped[str] = mapped_column(String, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class ConversationArchive(Base):
    """会话的完整执行归档，在在线数据被清理后继续保留。

    数据保留任务删除演示沙箱前，会为每个会话写入一条归档记录
    （见 `app/maintenance/archive.py`），使公开演示中的攻击尝试仍可在
    `RETENTION_DAYS` 天内复查。归档不设置外键，并完整保存所需信息，
    因为关联的客户、订单和消息届时已被删除。
    """

    __tablename__ = "conversation_archive"

    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    archived_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, index=True)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    # 本会话写入的退款记录：[{order_id, scenario, decision, amount}]。
    decisions: Mapped[list] = mapped_column(JSON, default=list)
    # 根据执行记录生成的排查标签，如 ["injection", "output_corrected"]。
    flags: Mapped[list] = mapped_column(JSON, default=list)
    # 根据订单场景和实际账本核验的违规项：[{kind, detail}]；为空表示未发现违规。
    breaches: Mapped[list] = mapped_column(JSON, default=list)
    breach: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    opik_trace_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 归档快照结构：{"orders": [...], "messages": [...], "events": [...]}。
    transcript: Mapped[dict] = mapped_column(JSON, default=dict)


ACTIVE_TICKET_STATUSES = ("OPEN", "WAITING_INFO", "PENDING_REVIEW", "APPROVED", "FAILED_RETRYABLE")
TICKET_STATUSES = ACTIVE_TICKET_STATUSES + ("COMPLETED", "REJECTED", "FAILED_TERMINAL", "CANCELLED")
_active_ticket_sql = "status IN ('OPEN','WAITING_INFO','PENDING_REVIEW','APPROVED','FAILED_RETRYABLE')"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    model_code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class AfterSalesTicket(Base):
    __tablename__ = "after_sales_tickets"
    __table_args__ = (
        Index("uq_tickets_active_order_type", "order_id", "type", unique=True,
              postgresql_where=text(_active_ticket_sql), sqlite_where=text(_active_ticket_sql)),
        CheckConstraint("type = 'exchange'", name="ck_ticket_type"),
        CheckConstraint("status IN (" + ",".join(repr(s) for s in TICKET_STATUSES) + ")",
                        name="ck_ticket_status"),
        CheckConstraint("reason_type IS NULL OR reason_type IN ('size','defect')", name="ck_ticket_reason"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    origin_conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"))
    workflow_thread_id: Mapped[str] = mapped_column(String, unique=True)
    type: Mapped[str] = mapped_column(String, default="exchange", server_default="exchange")
    reason_type: Mapped[str | None] = mapped_column(String)
    description: Mapped[str] = mapped_column(String(2000))
    requested_change: Mapped[str | None] = mapped_column(String(500))
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(500))


class TicketApproval(Base):
    __tablename__ = "ticket_approvals"
    __table_args__ = (CheckConstraint("decision IN ('approved','rejected')", name="ck_approval_decision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("after_sales_tickets.id"), unique=True)
    admin_subject: Mapped[str] = mapped_column(String)
    decision: Mapped[str] = mapped_column(String)
    comment: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class ExchangeAcceptance(Base):
    __tablename__ = "exchange_acceptances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("after_sales_tickets.id"), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    reference_code: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("after_sales_tickets.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    actor_type: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class TicketRequest(Base):
    __tablename__ = "ticket_requests"
    __table_args__ = (UniqueConstraint("ticket_id", "operation", "actor_type", "actor_subject", "request_id",
                                      name="uq_ticket_request_actor"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("after_sales_tickets.id"), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_subject: Mapped[str] = mapped_column(String)
    request_id: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class ConversationSummaryRecord(Base):
    __tablename__ = "conversation_summaries"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    summary_text: Mapped[str] = mapped_column(String)
    covered_message_id: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    model_code: Mapped[str | None] = mapped_column(String, index=True)
    revision: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    source_path: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String(64))
    indexed_at: Mapped[datetime | None] = mapped_column(TZDateTime())
