"""merge heads 9026 and 0104 (upstream v4.0.0)

Обе ветки независимо завели числовую панельную идентичность: наша `9026`
добавила `users.remnawave_id`, upstream в `0104` — тот же столбец плюс
`remnawave_id` в подписках и смежных таблицах. Конфликта при выполнении нет:
`0104` целиком inspector-guarded (каждый шаг проверяет наличие столбца перед
добавлением), поэтому на базе, где наша `9026` уже применена, он просто
пропустит готовые столбцы и доедет остальное.

Revision ID: 9027
Revises: 9026, 0104
Create Date: 2026-08-03

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '9027'
down_revision: Union[str, Sequence[str], None] = ('9026', '0104')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
