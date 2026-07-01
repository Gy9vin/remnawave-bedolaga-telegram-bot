"""add broadcast_history.blocked_user_ids

Revision ID: 0094
Revises: 0093
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0094'
down_revision: Union[str, None] = '0093'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('broadcast_history', sa.Column('blocked_user_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('broadcast_history', 'blocked_user_ids')
