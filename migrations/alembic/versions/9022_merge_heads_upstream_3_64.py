"""merge heads 9021 and 0096 (upstream v3.64.0)

Revision ID: 9022
Revises: 9021, 0096
Create Date: 2026-07-15

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9022'
down_revision: Union[str, Sequence[str], None] = ('9021', '0096')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
