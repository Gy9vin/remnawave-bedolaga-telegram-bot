"""merge heads 9013 and 0082 (upstream v3.55.0)

Revision ID: 9014
Revises: 9013, 0082
Create Date: 2026-05-14

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9014'
down_revision: Union[str, Sequence[str], None] = ('9013', '0082')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
