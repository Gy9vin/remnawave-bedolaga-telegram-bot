"""merge heads 9014 and 0085 (upstream v3.56.0)

Revision ID: 9015
Revises: 9014, 0085
Create Date: 2026-05-16

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9015'
down_revision: Union[str, Sequence[str], None] = ('9014', '0085')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
