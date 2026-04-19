"""merge heads 0060 and 9005 (upstream v3.49.0)

Revision ID: 9006
Revises: 0060, 9005
Create Date: 2026-04-19

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9006'
down_revision: Union[str, Sequence[str], None] = ('0060', '9005')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
