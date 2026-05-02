"""add free spin fields and probability_mode to wheel_configs

Revision ID: 9009
Revises: 9008
Create Date: 2026-05-02

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9009'
down_revision: Union[str, Sequence[str], None] = '9008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'wheel_configs',
        sa.Column('free_spin_enabled', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'wheel_configs',
        sa.Column('free_spins_per_period', sa.Integer(), nullable=False, server_default='1'),
    )
    op.add_column(
        'wheel_configs',
        sa.Column('free_spin_period_days', sa.Integer(), nullable=False, server_default='2'),
    )
    op.add_column(
        'wheel_configs',
        sa.Column(
            'free_spin_requires_active_subscription',
            sa.Boolean(),
            nullable=False,
            server_default='true',
        ),
    )
    op.add_column(
        'wheel_configs',
        sa.Column('probability_mode', sa.String(length=16), nullable=False, server_default='manual'),
    )


def downgrade() -> None:
    op.drop_column('wheel_configs', 'probability_mode')
    op.drop_column('wheel_configs', 'free_spin_requires_active_subscription')
    op.drop_column('wheel_configs', 'free_spin_period_days')
    op.drop_column('wheel_configs', 'free_spins_per_period')
    op.drop_column('wheel_configs', 'free_spin_enabled')
