"""merge heads 9017 and 0093 (upstream v3.61.0)

Revision ID: 9018
Revises: 9017, 0093
Create Date: 2026-06-20

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9018'
down_revision: Union[str, Sequence[str], None] = ('9017', '0093')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
