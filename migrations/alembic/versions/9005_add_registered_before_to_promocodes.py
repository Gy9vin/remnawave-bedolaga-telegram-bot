"""add registered_before to promocodes

Revision ID: 9005
Revises: 9004
Create Date: 2026-04-16

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9005'
down_revision: Union[str, None] = '9004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('promocodes', sa.Column('registered_before', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('promocodes', 'registered_before')
