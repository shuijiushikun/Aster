"""exchange tickets and product metadata

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22 10:05:38.596295
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("model_code", sa.String(), nullable=True),
        sa.Column("revision", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("knowledge_documents", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_knowledge_documents_model_code"), ["model_code"], unique=False
        )

    op.create_table(
        "products",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("model_code", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("(true)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_code"),
    )
    op.create_table(
        "after_sales_tickets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("origin_conversation_id", sa.String(), nullable=True),
        sa.Column("workflow_thread_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), server_default="exchange", nullable=False),
        sa.Column("reason_type", sa.String(), nullable=True),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("requested_change", sa.String(length=500), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "reason_type IS NULL OR reason_type IN ('size','defect')", name="ck_ticket_reason"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','WAITING_INFO','PENDING_REVIEW','APPROVED','FAILED_RETRYABLE','COMPLETED','REJECTED','FAILED_TERMINAL','CANCELLED')",
            name="ck_ticket_status",
        ),
        sa.CheckConstraint("type = 'exchange'", name="ck_ticket_type"),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["origin_conversation_id"],
            ["conversations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_thread_id"),
    )
    with op.batch_alter_table("after_sales_tickets", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_after_sales_tickets_customer_id"), ["customer_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_after_sales_tickets_order_id"), ["order_id"], unique=False
        )
        batch_op.create_index(
            "uq_tickets_active_order_type",
            ["order_id", "type"],
            unique=True,
            postgresql_where=sa.text(
                "status IN ('OPEN','WAITING_INFO','PENDING_REVIEW','APPROVED','FAILED_RETRYABLE')"
            ),
            sqlite_where=sa.text(
                "status IN ('OPEN','WAITING_INFO','PENDING_REVIEW','APPROVED','FAILED_RETRYABLE')"
            ),
        )

    op.create_table(
        "conversation_summaries",
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("summary_text", sa.String(), nullable=False),
        sa.Column("covered_message_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
        ),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    with op.batch_alter_table("conversation_summaries", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_conversation_summaries_customer_id"), ["customer_id"], unique=False
        )

    op.create_table(
        "exchange_acceptances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("reference_code", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["after_sales_tickets.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("order_id"),
        sa.UniqueConstraint("reference_code"),
        sa.UniqueConstraint("ticket_id"),
    )
    op.create_table(
        "ticket_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("admin_subject", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('approved','rejected')", name="ck_approval_decision"),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["after_sales_tickets.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_id"),
    )
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("actor_type", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["after_sales_tickets.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ticket_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ticket_events_ticket_id"), ["ticket_id"], unique=False)

    op.create_table(
        "ticket_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_subject", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["after_sales_tickets.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ticket_id",
            "operation",
            "actor_type",
            "actor_subject",
            "request_id",
            name="uq_ticket_request_actor",
        ),
    )
    with op.batch_alter_table("ticket_requests", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_ticket_requests_ticket_id"), ["ticket_id"], unique=False
        )

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("product_id", sa.String(), nullable=True))
        batch_op.create_foreign_key("fk_orders_product_id", "products", ["product_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_constraint("fk_orders_product_id", type_="foreignkey")
        batch_op.drop_column("product_id")

    with op.batch_alter_table("ticket_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ticket_requests_ticket_id"))

    op.drop_table("ticket_requests")
    with op.batch_alter_table("ticket_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ticket_events_ticket_id"))

    op.drop_table("ticket_events")
    op.drop_table("ticket_approvals")
    op.drop_table("exchange_acceptances")
    with op.batch_alter_table("conversation_summaries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversation_summaries_customer_id"))

    op.drop_table("conversation_summaries")
    with op.batch_alter_table("after_sales_tickets", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_tickets_active_order_type",
            postgresql_where=sa.text(
                "status IN ('OPEN','WAITING_INFO','PENDING_REVIEW','APPROVED','FAILED_RETRYABLE')"
            ),
            sqlite_where=sa.text(
                "status IN ('OPEN','WAITING_INFO','PENDING_REVIEW','APPROVED','FAILED_RETRYABLE')"
            ),
        )
        batch_op.drop_index(batch_op.f("ix_after_sales_tickets_order_id"))
        batch_op.drop_index(batch_op.f("ix_after_sales_tickets_customer_id"))

    op.drop_table("after_sales_tickets")
    op.drop_table("products")
    with op.batch_alter_table("knowledge_documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_knowledge_documents_model_code"))

    op.drop_table("knowledge_documents")
