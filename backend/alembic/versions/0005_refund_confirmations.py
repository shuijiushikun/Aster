"""Persist customer confirmation snapshots before refund execution."""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "refund_confirmations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("customer_id", sa.String(), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.String(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("customer_id", "request_id", name="uq_refund_confirmation_request"),
        sa.CheckConstraint("status IN ('pending','confirmed','cancelled','expired','stale')",
                           name="ck_refund_confirmation_status"),
    )
    op.create_index("ix_refund_confirmation_conversation", "refund_confirmations", ["conversation_id", "status"])


def downgrade():
    op.drop_table("refund_confirmations")
