"""Резолв панельной идентичности пользователя для кабинета.

Живёт отдельно от эндпоинтов, потому что нужен и пользовательской части
кабинета, и админской: обе спрашивают панель про одного и того же человека и
обе одинаково слепнут, когда числовой id панели ещё не проставлен.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, User


logger = structlog.get_logger(__name__)


def _identity_holder(
    subscription: Subscription | None,
    user: User,
    prefer_subscription: bool,
) -> Subscription | User:
    """На какой записи лежит панельная идентичность для этого вызова.

    Мульти-тариф: каждая подписка — СВОЙ панельный пользователь, поэтому id
    берётся с подписки и не откатывается на ``user.remnawave_id``, когда пуст.
    Откат читал бы панельного юзера другого тарифа, из-за чего устройства и
    лимит HWID выглядели общими на все тарифы (баг с общим лимитом «по
    наименьшему тарифу»).

    ``prefer_subscription`` — для админских маршрутов: там подписку выбирают
    явным параметром запроса, и этот выбор старше режима тарификации. Но только
    пока на подписке действительно есть id: в однотарифном режиме идентичность
    канонически лежит на юзере, а колонка у подписки чаще всего пуста. Админка
    шлёт subscription_id всегда, и слепое чтение пустой колонки давало пустой
    список устройств при живом аккаунте в панели — ровно тот баг, из-за
    которого этот модуль и появился.
    """
    if subscription is None:
        return user
    if settings.is_multi_tariff_enabled():
        return subscription
    if prefer_subscription and subscription.remnawave_id:
        return subscription
    return user


def resolve_panel_user_id(
    subscription: Subscription | None,
    user: User,
    *,
    prefer_subscription: bool = False,
) -> int | None:
    """Числовой id панельного пользователя, уже сохранённый в базе."""
    return _identity_holder(subscription, user, prefer_subscription).remnawave_id


async def ensure_panel_user_id(
    db: AsyncSession,
    subscription: Subscription | None,
    user: User,
    api: Any,
    *,
    prefer_subscription: bool = False,
) -> int | None:
    """Как ``resolve_panel_user_id``, но дорезолвит id, если он ещё не сохранён.

    Переход на RemnaWave 3.x заменил строковый uuid панельного пользователя на
    числовой id, а миграция колонку только заводит — заполняет её отдельный
    бэкфилл. До его прогона у всех записей, созданных раньше, id пустой, и
    список устройств выглядел пустым, хотя в панели устройства на месте:
    спросить панель было не по чему.

    Поэтому при пустом id идём в панель сами — по short_uuid подписки (точный
    ключ, панель хранит его как есть) и, если его нет, по telegram_id. Совпадение
    по telegram_id принимаем только когда оно однозначно: несколько панельных
    аккаунтов на один telegram_id означают мульти-тариф, и угадывать там нельзя —
    чужой аккаунт показал бы человеку не его устройства.

    Найденный id сразу сохраняется, так что поход в панель случается один раз на
    запись. Ошибка сохранения не должна ронять выдачу списка — id уже известен и
    в этом запросе используется, а в следующий раз резолв просто повторится.
    """
    holder = _identity_holder(subscription, user, prefer_subscription)
    panel_user_id = holder.remnawave_id
    if panel_user_id:
        return panel_user_id

    short_uuid = getattr(subscription, 'remnawave_short_uuid', None) if subscription else None
    resolved_id: int | None = None
    try:
        if short_uuid:
            resolved = await api.resolve_user(short_uuid=short_uuid)
            resolved_id = (resolved or {}).get('id')
        if not resolved_id and user.telegram_id:
            candidates = await api.find_users_by_telegram_id(user.telegram_id)
            unique_ids = {c.id for c in candidates or [] if getattr(c, 'id', None)}
            if len(unique_ids) == 1:
                resolved_id = unique_ids.pop()
    except Exception as error:
        logger.warning(
            'Lazy panel identity resolve failed',
            user_id=user.id,
            subscription_id=getattr(subscription, 'id', None),
            error=str(error)[:200],
        )
        return None

    if not resolved_id:
        return None

    # Пишем ровно туда, откуда читали, иначе следующий запрос снова пойдёт
    # в панель — а при выбранной подписке ещё и записал бы её id юзеру.
    holder.remnawave_id = resolved_id
    try:
        await db.commit()
    except Exception as error:
        logger.warning(
            'Lazy panel identity resolved but not persisted',
            user_id=user.id,
            remnawave_id=resolved_id,
            error=str(error)[:200],
        )
    logger.info(
        'Lazy panel identity resolved',
        user_id=user.id,
        subscription_id=getattr(subscription, 'id', None),
        remnawave_id=resolved_id,
    )
    return resolved_id
