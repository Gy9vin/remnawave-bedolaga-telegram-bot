"""change users.pre_penalty_squads from JSON to JSONB

Без JSONB Postgres не имеет оператора равенства для типа json — это ломает
любой SELECT DISTINCT по таблице users (например в _check_low_balance_alerts).

Revision ID: 9011
Revises: 9010
Create Date: 2026-05-03

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '9011'
down_revision: Union[str, Sequence[str], None] = '9010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'users',
        'pre_penalty_squads',
        existing_type=sa.JSON(),
        type_=postgresql.JSONB(),
        existing_nullable=True,
        postgresql_using='pre_penalty_squads::jsonb',
    )


def downgrade() -> None:
    op.alter_column(
        'users',
        'pre_penalty_squads',
        existing_type=postgresql.JSONB(),
        type_=sa.JSON(),
        existing_nullable=True,
        postgresql_using='pre_penalty_squads::json',
    )
