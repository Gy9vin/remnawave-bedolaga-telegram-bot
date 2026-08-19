"""Add subscription freeze fields

Revision ID: 9030
Revises: 9029
Create Date: 2026-08-19

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '9030'
down_revision: Union[str, Sequence[str], None] = '9029'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('is_frozen', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'subscriptions',
        sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('frozen_days_banked', sa.Integer(), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('frozen_auto_unfreeze_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'ix_subscriptions_frozen_auto_unfreeze',
        'subscriptions',
        ['is_frozen', 'frozen_auto_unfreeze_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_subscriptions_frozen_auto_unfreeze', table_name='subscriptions')
    op.drop_column('subscriptions', 'frozen_auto_unfreeze_at')
    op.drop_column('subscriptions', 'frozen_days_banked')
    op.drop_column('subscriptions', 'frozen_at')
    op.drop_column('subscriptions', 'is_frozen')
