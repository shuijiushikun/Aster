"""Persist MCP write request receipts without changing existing business rows."""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_tool_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("customer_id", "operation", "request_id", name="uq_agent_tool_request"),
    )


def downgrade():
    op.drop_table("agent_tool_requests")
