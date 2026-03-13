"""Add AI forum support columns to tickets and ticket_messages

New columns for AI-powered ticket system with Telegram forum mirroring:
  - tickets.forum_topic_id       — ID темы в форуме
  - tickets.forum_control_msg_id — ID сообщения с кнопками управления
  - tickets.ai_enabled           — AI включён для этого тикета
  - tickets.operator_telegram_id — Telegram ID взявшего оператора
  - ticket_messages.is_ai_response — помечает ответы сгенерированные AI

Revision ID: 0043
Revises: 0042
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = '0043'
down_revision: Union[str, None] = '0042'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in cols


def upgrade() -> None:
    if not _column_exists('tickets', 'forum_topic_id'):
        op.add_column('tickets', sa.Column('forum_topic_id', sa.Integer(), nullable=True))

    if not _column_exists('tickets', 'forum_control_msg_id'):
        op.add_column('tickets', sa.Column('forum_control_msg_id', sa.Integer(), nullable=True))

    if not _column_exists('tickets', 'ai_enabled'):
        op.add_column(
            'tickets',
            sa.Column('ai_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        )

    if not _column_exists('tickets', 'operator_telegram_id'):
        op.add_column('tickets', sa.Column('operator_telegram_id', sa.BigInteger(), nullable=True))

    if not _column_exists('ticket_messages', 'is_ai_response'):
        op.add_column(
            'ticket_messages',
            sa.Column('is_ai_response', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )


def downgrade() -> None:
    op.drop_column('ticket_messages', 'is_ai_response')
    op.drop_column('tickets', 'operator_telegram_id')
    op.drop_column('tickets', 'ai_enabled')
    op.drop_column('tickets', 'forum_control_msg_id')
    op.drop_column('tickets', 'forum_topic_id')
