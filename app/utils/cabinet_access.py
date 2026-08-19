"""Утилита проверки доступа к веб-кабинету.

Логика доступа: user.cabinet_access OR settings.CABINET_OPEN_TO_ALL.

  - cabinet_access=True  → пользователь включён в бету индивидуально.
  - CABINET_OPEN_TO_ALL=True → кабинет открыт для всех без редеплоя
    (переключается через admin-UI системных настроек).

Функция используется и серверным гейтом (cabinet/dependencies.py),
и клавиатурой бота (keyboards/inline.py) — вызывается синхронно,
т.к. оба значения уже загружены (поле ORM-объекта + settings-синглтон).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import settings


if TYPE_CHECKING:
    from app.database.models import User


def has_cabinet_access(user: 'User') -> bool:
    """True если пользователь имеет доступ к веб-кабинету.

    Args:
        user: ORM-объект User с загруженным полем cabinet_access.

    Returns:
        True если user.cabinet_access=True ИЛИ CABINET_OPEN_TO_ALL=True.
    """
    return bool(user.cabinet_access) or bool(settings.CABINET_OPEN_TO_ALL)
