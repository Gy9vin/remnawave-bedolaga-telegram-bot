"""add users.remnawave_id (BigInteger, nullable) для поддержки RemnaWave v3

RemnaWave v3 идентифицирует пользователей числовым id вместо UUID.
Колонка хранит этот id рядом с remnawave_uuid; nullable — v2-пользователи
получат значение позже при backfill. Индекс нужен для быстрого lookup
при синхронизации подписок.

Revision ID: 9026
Revises: 9025
Create Date: 2026-06-30

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9026'
down_revision: Union[str, Sequence[str], None] = '9025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('remnawave_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_users_remnawave_id', 'users', ['remnawave_id'])


def downgrade() -> None:
    op.drop_index('ix_users_remnawave_id', table_name='users')
    op.drop_column('users', 'remnawave_id')
