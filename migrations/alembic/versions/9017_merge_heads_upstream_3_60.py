"""merge heads 9016 and 0089 (upstream v3.60.0)

Revision ID: 9017
Revises: 9016, 0089
Create Date: 2026-06-06

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9017'
down_revision: Union[str, Sequence[str], None] = ('9016', '0089')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
