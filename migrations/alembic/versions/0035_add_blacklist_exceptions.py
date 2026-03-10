"""Add blacklist_exceptions table

Revision ID: 0035
Revises: 0034
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0035'
down_revision: Union[str, None] = '0034'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return insp.has_table(table_name)


def upgrade() -> None:
    if not _table_exists('blacklist_exceptions'):
        op.create_table(
            'blacklist_exceptions',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('telegram_id', sa.BigInteger(), nullable=False, unique=True, index=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    if _table_exists('blacklist_exceptions'):
        op.drop_table('blacklist_exceptions')
