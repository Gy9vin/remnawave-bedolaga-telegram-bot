"""add is_penalized to users

Revision ID: 9008
Revises: 9007
Create Date: 2026-04-28

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9008'
down_revision: Union[str, Sequence[str], None] = '9007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('is_penalized', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('users', 'is_penalized')
