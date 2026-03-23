"""add personal_price_multiplier to users

Revision ID: 9001
Revises: 0049
Create Date: 2026-03-18

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text

revision: str = '9001'
down_revision: Union[str, None] = '0049'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c['name'] for c in inspector.get_columns('users')]
    if 'personal_price_multiplier' not in columns:
        op.execute(
            text(
                "ALTER TABLE users ADD COLUMN personal_price_multiplier FLOAT NOT NULL DEFAULT 1.0"
            )
        )


def downgrade() -> None:
    op.drop_column('users', 'personal_price_multiplier')
