"""merge heads 0067 and 9006 (upstream v3.52.0)

Revision ID: 9007
Revises: 0067, 9006
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9007'
down_revision: Union[str, Sequence[str], None] = ('0067', '9006')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
