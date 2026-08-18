"""колонка users.cabinet_ui_mode — персональный выбор интерфейса кабинета

Хранит 'simple' или 'advanced'. NULL — «человек не выбирал»: он слушает
глобальный флаг CABINET_LITE_MODE_ENABLED и подхватит его смену. Поэтому
бэкфилла нет и server_default не ставится: проставить всем 'advanced' значило
бы навсегда отрезать существующую базу от глобального переключателя.

Revision ID: 9029
Revises: 9028
Create Date: 2026-08-18

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '9029'
down_revision: Union[str, Sequence[str], None] = '9028'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('cabinet_ui_mode', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'cabinet_ui_mode')
