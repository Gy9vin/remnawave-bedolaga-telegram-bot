"""add expiry/traffic fallback fields to subscriptions

Поля для механики «fallback-сквад при истечении/исчерпании трафика».
При истечении подписки юзер не отключается полностью, а переезжает в
специальный сквад (Telegram-only), сохраняя оригинальные значения, чтобы
вернуть всё при продлении.

Revision ID: 9012
Revises: 9011
Create Date: 2026-05-07

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '9012'
down_revision: Union[str, Sequence[str], None] = '9011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('expiry_fallback_active', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'subscriptions',
        sa.Column('expiry_fallback_started_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('traffic_fallback_active', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'subscriptions',
        sa.Column('pre_expiry_squads', postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('pre_expiry_traffic_limit_bytes', sa.BigInteger(), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('pre_expiry_expire_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'ix_subscriptions_expiry_fallback_active',
        'subscriptions',
        ['expiry_fallback_active'],
        postgresql_where=sa.text('expiry_fallback_active IS true'),
    )


def downgrade() -> None:
    op.drop_index('ix_subscriptions_expiry_fallback_active', table_name='subscriptions')
    op.drop_column('subscriptions', 'pre_expiry_expire_at')
    op.drop_column('subscriptions', 'pre_expiry_traffic_limit_bytes')
    op.drop_column('subscriptions', 'pre_expiry_squads')
    op.drop_column('subscriptions', 'traffic_fallback_active')
    op.drop_column('subscriptions', 'expiry_fallback_started_at')
    op.drop_column('subscriptions', 'expiry_fallback_active')
