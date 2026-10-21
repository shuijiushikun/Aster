"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-24 17:20:02.505802
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('customers',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('loyalty_tier', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_customers_email'), 'customers', ['email'], unique=True)
    op.create_table('conversations',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('customer_id', sa.String(), nullable=True),
    sa.Column('event_seq', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tokens_used', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_conversations_created_at', 'conversations', ['created_at'], unique=False)
    op.create_index('ix_conversations_customer_created', 'conversations', ['customer_id', 'created_at'], unique=False)
    op.create_table('orders',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('customer_id', sa.String(), nullable=False),
    sa.Column('product_name', sa.String(), nullable=False),
    sa.Column('category', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('order_date', sa.DateTime(timezone=True), nullable=False),
    sa.Column('delivered_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_final_sale', sa.Boolean(), nullable=False),
    sa.Column('refunded', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_orders_customer_id'), 'orders', ['customer_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('conversation_id', sa.String(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('content', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messages_conversation_id'), 'messages', ['conversation_id'], unique=False)
    op.create_table('reasoning_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('conversation_id', sa.String(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=True),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('step_type', sa.String(), nullable=False),
    sa.Column('node', sa.String(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_id', 'seq', name='uq_reasoning_events_conv_seq')
    )
    op.create_index(op.f('ix_reasoning_events_conversation_id'), 'reasoning_events', ['conversation_id'], unique=False)
    op.create_table('refunds',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('order_id', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('decision', sa.String(), nullable=False),
    sa.Column('reason', sa.String(), nullable=False),
    sa.Column('decided_by', sa.String(), nullable=False),
    sa.Column('conversation_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_refunds_conversation_id'), 'refunds', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_refunds_order_id'), 'refunds', ['order_id'], unique=False)
    op.create_index('uq_refunds_one_approved_per_order', 'refunds', ['order_id'], unique=True, postgresql_where=sa.text("decision = 'approved'"), sqlite_where=sa.text("decision = 'approved'"))


def downgrade() -> None:
    op.drop_index('uq_refunds_one_approved_per_order', table_name='refunds', postgresql_where=sa.text("decision = 'approved'"), sqlite_where=sa.text("decision = 'approved'"))
    op.drop_index(op.f('ix_refunds_order_id'), table_name='refunds')
    op.drop_index(op.f('ix_refunds_conversation_id'), table_name='refunds')
    op.drop_table('refunds')
    op.drop_index(op.f('ix_reasoning_events_conversation_id'), table_name='reasoning_events')
    op.drop_table('reasoning_events')
    op.drop_index(op.f('ix_messages_conversation_id'), table_name='messages')
    op.drop_table('messages')
    op.drop_index(op.f('ix_orders_customer_id'), table_name='orders')
    op.drop_table('orders')
    op.drop_index('ix_conversations_customer_created', table_name='conversations')
    op.drop_index('ix_conversations_created_at', table_name='conversations')
    op.drop_table('conversations')
    op.drop_index(op.f('ix_customers_email'), table_name='customers')
    op.drop_table('customers')
