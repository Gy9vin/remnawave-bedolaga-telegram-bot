"""add broadcast_history.blocked_user_ids

Revision ID: 9021
Revises: 9020
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9021'
down_revision: Union[str, None] = '9020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('broadcast_history', sa.Column('blocked_user_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('broadcast_history', 'blocked_user_ids')
