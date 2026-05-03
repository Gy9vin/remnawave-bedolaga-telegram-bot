"""add pre_penalty_squads to users

Revision ID: 9010
Revises: 9009
Create Date: 2026-05-03

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9010'
down_revision: Union[str, Sequence[str], None] = '9009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('pre_penalty_squads', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'pre_penalty_squads')
