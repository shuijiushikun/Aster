"""conversation archive

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13 18:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('conversation_archive',
    sa.Column('conversation_id', sa.String(), nullable=False),
    sa.Column('customer_id', sa.String(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('turns', sa.Integer(), nullable=False),
    sa.Column('tokens_used', sa.Integer(), nullable=False),
    sa.Column('decisions', sa.JSON(), nullable=False),
    sa.Column('flags', sa.JSON(), nullable=False),
    sa.Column('breaches', sa.JSON(), nullable=False),
    sa.Column('breach', sa.Boolean(), nullable=False),
    sa.Column('opik_trace_ids', sa.JSON(), nullable=False),
    sa.Column('transcript', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('conversation_id')
    )
    op.create_index(op.f('ix_conversation_archive_customer_id'), 'conversation_archive', ['customer_id'], unique=False)
    op.create_index(op.f('ix_conversation_archive_started_at'), 'conversation_archive', ['started_at'], unique=False)
    op.create_index(op.f('ix_conversation_archive_archived_at'), 'conversation_archive', ['archived_at'], unique=False)
    op.create_index(op.f('ix_conversation_archive_breach'), 'conversation_archive', ['breach'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_conversation_archive_breach'), table_name='conversation_archive')
    op.drop_index(op.f('ix_conversation_archive_archived_at'), table_name='conversation_archive')
    op.drop_index(op.f('ix_conversation_archive_started_at'), table_name='conversation_archive')
    op.drop_index(op.f('ix_conversation_archive_customer_id'), table_name='conversation_archive')
    op.drop_table('conversation_archive')
