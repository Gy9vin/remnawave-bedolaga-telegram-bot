"""merge heads 0074 and 9012 (upstream v3.54.0)

Revision ID: 9013
Revises: 0074, 9012
Create Date: 2026-05-10

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9013'
down_revision: Union[str, Sequence[str], None] = ('0074', '9012')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
