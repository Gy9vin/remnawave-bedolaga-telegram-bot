"""Поиск человека, за которого платят.

Плательщик вводит то, что знает: @ник, telegram id или email. Тип определяем
сами — заставлять выбирать его руками значит плодить вопросы в поддержку. @ник
годится как основной способ: ``users.username`` обновляется при каждом касании
бота (``middlewares/auth.py``), так что он свежий у всех активных.

Отказ всегда выглядит одинаково — ``None``, «не найдено». Заблокированный,
удалённый и забаненный неотличимы от несуществующего: иначе перебором можно
выяснять, кто наш клиент и кого мы забанили.
"""

from __future__ import annotations

import re

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, UserStatus


logger = structlog.get_logger(__name__)

# Ник в Telegram: буквы, цифры и подчёркивания. Точку намеренно не пускаем —
# по ней отличаем email от ника.
_USERNAME_RE = re.compile(r'^@?([A-Za-z0-9_]{1,64})$')
_TELEGRAM_ID_RE = re.compile(r'^\d{5,20}$')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


class RecipientIsPayerError(Exception):
    """Плательщик указал сам себя — для этого есть обычное продление."""


def classify_query(raw: str | None) -> tuple[str, str] | None:
    """Определить, что ввёл плательщик: ``('username'|'telegram_id'|'email', значение)``.

    Возвращает ``None`` для мусора, чтобы вызывающий не ходил в базу впустую.
    """
    if not raw:
        return None
    query = raw.strip()
    if not query:
        return None

    if _EMAIL_RE.match(query):
        return ('email', query)
    if _TELEGRAM_ID_RE.match(query):
        return ('telegram_id', query)
    match = _USERNAME_RE.match(query)
    if match:
        return ('username', match.group(1))
    return None


async def _find_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(func.lower(User.username) == username.lower()))
    return result.scalars().first()


async def _find_by_telegram_id(db: AsyncSession, telegram_id: str) -> User | None:
    result = await db.execute(select(User).where(User.telegram_id == int(telegram_id)))
    return result.scalars().first()


async def _find_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalars().first()


async def _is_blacklisted(user: User) -> bool:
    if user.telegram_id is None:
        return False
    from app.services.blacklist_service import blacklist_service

    try:
        is_banned, _reason = await blacklist_service.is_user_blacklisted(user.telegram_id, user.username)
    except Exception as error:
        # Сбой самой проверки не повод пускать: платить забаненному хуже, чем
        # отказать лишний раз.
        logger.warning('Проверка чёрного списка недоступна', user_id=user.id, error=str(error)[:200])
        return True
    return bool(is_banned)


async def resolve_recipient(db: AsyncSession, query: str, *, payer_id: int) -> User | None:
    """Найти получателя платежа. ``None`` — «не найдено» по любой причине."""
    classified = classify_query(query)
    if classified is None:
        return None

    kind, value = classified
    finder = {
        'username': _find_by_username,
        'telegram_id': _find_by_telegram_id,
        'email': _find_by_email,
    }[kind]

    user = await finder(db, value)
    if user is None:
        return None

    if user.id == payer_id:
        # Единственный случай, о котором говорим прямо: это не чужой аккаунт, и
        # скрывать нечего, а человеку надо подсказать обычное продление.
        raise RecipientIsPayerError

    if user.status != UserStatus.ACTIVE.value:
        return None

    if await _is_blacklisted(user):
        return None

    return user
