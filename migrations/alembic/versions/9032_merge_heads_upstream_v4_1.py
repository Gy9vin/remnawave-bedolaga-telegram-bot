"""merge heads 9031 and 0106 (upstream v4.1.0)

Мерж upstream 4.1.0 в форк. Наша ветка дошла до `9031`
(`users.cabinet_access`), upstream независимо добавил `0105`
(`promocodes.traffic_gb`) и `0106` (привязка гостевой покупки к рекламной
кампании) поверх общего предка `0104`. Обе новые upstream-миграции
inspector-/existence-guarded и с нашей 9xxx-линией по данным не пересекаются,
поэтому конфликта при выполнении нет — эта ревизия лишь сводит две головы в
одну, чтобы `alembic upgrade head` был однозначным.

Revision ID: 9032
Revises: 9031, 0106
Create Date: 2026-08-20

"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = '9032'
down_revision: Union[str, Sequence[str], None] = ('9031', '0106')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
