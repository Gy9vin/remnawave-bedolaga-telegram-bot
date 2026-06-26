"""add user_clients table (user → VPN client app mapping)

Maps each user to the VPN client application(s) they use, derived from
HWID device userAgent strings pulled from RemnaWave. One row per
(user_id, app_name) pair, refreshed by the client-sync scheduler.
Used to target broadcasts at users of a specific client app (Happ,
v2rayNG, Streisand, …).

Revision ID: 9020
Revises: 9019
Create Date: 2026-06-26

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9020'
down_revision: Union[str, Sequence[str], None] = '9019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_clients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('app_name', sa.String(length=64), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'app_name', name='uq_user_clients_user_app'),
    )
    op.create_index('ix_user_clients_user_id', 'user_clients', ['user_id'])
    op.create_index('ix_user_clients_app_name', 'user_clients', ['app_name'])


def downgrade() -> None:
    op.drop_index('ix_user_clients_app_name', table_name='user_clients')
    op.drop_index('ix_user_clients_user_id', table_name='user_clients')
    op.drop_table('user_clients')
