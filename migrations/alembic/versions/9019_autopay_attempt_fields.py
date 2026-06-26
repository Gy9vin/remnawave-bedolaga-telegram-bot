"""add autopay attempt/result tracking fields to subscriptions

Поля для отслеживания попыток автопродления: когда была попытка, статус,
ошибка, время успешного продления и выбранный период.

Revision ID: 9019
Revises: 9018
Create Date: 2026-06-26

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9019'
down_revision: Union[str, Sequence[str], None] = '9018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('last_autopay_attempt_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('last_autopay_status', sa.String(32), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('last_autopay_error', sa.String(512), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('last_autopay_renewed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('last_autopay_period_days', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('subscriptions', 'last_autopay_period_days')
    op.drop_column('subscriptions', 'last_autopay_renewed_at')
    op.drop_column('subscriptions', 'last_autopay_error')
    op.drop_column('subscriptions', 'last_autopay_status')
    op.drop_column('subscriptions', 'last_autopay_attempt_at')
