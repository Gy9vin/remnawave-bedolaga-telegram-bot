"""merge heads 0057 and 9003

Revision ID: 9004
Revises: 0057, 9003
Create Date: 2026-04-15

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9004'
down_revision: Union[str, Sequence[str], None] = ('0057', '9003')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
