"""merge heads 9015 and 0087 (upstream v3.57.0)

Revision ID: 9016
Revises: 9015, 0087
Create Date: 2026-05-29

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9016'
down_revision: Union[str, Sequence[str], None] = ('9015', '0087')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
