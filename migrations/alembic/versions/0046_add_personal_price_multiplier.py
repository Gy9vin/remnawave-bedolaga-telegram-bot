"""add personal_price_multiplier to users

Revision ID: 0046
Revises: 0045
Create Date: 2026-03-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0046'
down_revision: Union[str, None] = '0045'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('personal_price_multiplier', sa.Float(), nullable=False, server_default='1.0'),
    )


def downgrade() -> None:
    op.drop_column('users', 'personal_price_multiplier')
