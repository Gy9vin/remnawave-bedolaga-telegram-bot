"""merge heads 9023 and 0103 (upstream v3.67)

Revision ID: 9024
Revises: 9023, 0103
Create Date: 2026-07-30

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9024'
down_revision: Union[str, Sequence[str], None] = ('9023', '0103')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
