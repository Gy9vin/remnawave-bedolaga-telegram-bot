"""Add show_in_gift to tariffs and blacklist_exceptions table

For servers that ran our custom 0038 (blacklist_exceptions):
  - show_in_gift was never added by upstream's 0038 — add it here
  - blacklist_exceptions already exists — skip with IF NOT EXISTS check

Revision ID: 0042
Revises: 0041
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = '0042'
down_revision: Union[str, None] = '0041'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in cols


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table_name)


def upgrade() -> None:
    # tariffs.show_in_gift — upstream migration 0038 won't run on existing servers
    # because our old 0038 (blacklist_exceptions) was already applied there
    if not _column_exists('tariffs', 'show_in_gift'):
        op.add_column(
            'tariffs',
            sa.Column('show_in_gift', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        )

    # blacklist_exceptions — create for fresh installs that never had our old 0038
    if not _table_exists('blacklist_exceptions'):
        op.create_table(
            'blacklist_exceptions',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('telegram_id', sa.BigInteger(), nullable=False, unique=True, index=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    pass
