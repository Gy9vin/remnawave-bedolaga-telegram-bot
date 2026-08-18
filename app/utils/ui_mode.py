"""Эффективный режим интерфейса кабинета.

Два уровня. Персональный выбор человека (`User.cabinet_ui_mode`) сильнее
глобального дефолта (системный флаг CABINET_LITE_MODE_ENABLED). Глобальный флаг
действует только на тех, кто ничего не выбирал, — благодаря этому простой режим
можно включить всей базе одним тумблером и так же откатить, не затерев выбор
тех, кто осознанно вернулся на полный интерфейс.
"""

from __future__ import annotations

UI_MODE_SIMPLE = 'simple'
UI_MODE_ADVANCED = 'advanced'
UI_MODES: tuple[str, str] = (UI_MODE_SIMPLE, UI_MODE_ADVANCED)


def normalize_ui_mode(value: object) -> str | None:
    """Привести значение к допустимому режиму или к None.

    None возвращается и для «не выбирал», и для мусора: снаружи оба случая
    означают одно — слушать глобальный дефолт. Мусор в колонке не должен
    ронять кабинет и не должен молча трактоваться как 'simple'.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in UI_MODES else None


def resolve_ui_mode(user_choice: object, *, lite_mode_enabled: bool) -> str:
    """Вернуть режим, в котором кабинет должен отрисоваться прямо сейчас.

    Возвращает всегда одну из двух строк — наружу None не выходит, чтобы
    потребителям не приходилось повторять правило дефолта у себя.
    """
    normalized = normalize_ui_mode(user_choice)
    if normalized is not None:
        return normalized
    return UI_MODE_SIMPLE if lite_mode_enabled else UI_MODE_ADVANCED
