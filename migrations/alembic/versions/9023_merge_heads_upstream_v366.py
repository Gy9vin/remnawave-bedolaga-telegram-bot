"""merge heads 9022 and 0100 (upstream v3.66)

Revision ID: 9023
Revises: 9022, 0100
Create Date: 2026-07-27

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9023'
down_revision: Union[str, Sequence[str], None] = ('9022', '0100')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
