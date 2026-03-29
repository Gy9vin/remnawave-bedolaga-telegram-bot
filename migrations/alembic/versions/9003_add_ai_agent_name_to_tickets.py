"""Add ai_agent_name to tickets

Revision ID: 9003
Revises: 9002
Create Date: 2026-03-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = '9003'
down_revision: Union[str, Sequence[str], None] = ('9002', '0052')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return False
    return column_name in [c['name'] for c in insp.get_columns(table_name)]


def upgrade() -> None:
    if not _column_exists('tickets', 'ai_agent_name'):
        op.add_column('tickets', sa.Column('ai_agent_name', sa.String(64), nullable=True))


def downgrade() -> None:
    pass
