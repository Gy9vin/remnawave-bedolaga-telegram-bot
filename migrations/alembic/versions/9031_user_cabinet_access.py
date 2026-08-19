"""колонка users.cabinet_access — per-user доступ к веб-кабинету

False по умолчанию: при бета-раскатке только пользователи с cabinet_access=True
могут войти в кабинет (если глобальный флаг CABINET_OPEN_TO_ALL выключен).
При массовом открытии достаточно включить CABINET_OPEN_TO_ALL через admin-UI,
не меняя значения в таблице users.

Revision ID: 9031
Revises: 9030
Create Date: 2026-08-19

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '9031'
down_revision: Union[str, Sequence[str], None] = '9030'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('cabinet_access', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('users', 'cabinet_access')
