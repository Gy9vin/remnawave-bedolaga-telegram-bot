"""Fire-and-forget доставка админских уведомлений из HTTP-обработчиков ЛК.

Зачем: создание Bot + отправка через Telegram могут занять секунды-минуты
(если в окружении SOCKS5 прокси флакает или сеть до Telegram нестабильна).
Если ждать этого синхронно перед HTTP-ответом юзеру — фронт ловит timeout,
хотя покупка уже выполнилась.

Используется только в endpoint-ах кабинета. Не использовать в местах, где
нужна доставка ДО завершения транзакции/HTTP-ответа.

Фоновая задача создаёт собственную AsyncSession и перечитывает объекты по ID,
чтобы не зависеть от закрытой сессии исходного HTTP-запроса.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


logger = structlog.get_logger(__name__)

# Храним ссылки на запущенные фоновые задачи, чтобы их не прибил GC
_pending_bg_tasks: set[asyncio.Task] = set()


def _schedule_bg(coro: Awaitable[Any]) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        return
    _pending_bg_tasks.add(task)
    task.add_done_callback(_pending_bg_tasks.discard)


def dispatch_subscription_purchase_notification_bg(
    *,
    user_id: int,
    subscription_id: int,
    transaction_id: int | None,
    period_days: int,
    was_trial_conversion: bool,
    amount_kopeks: int,
    purchase_type: str,
) -> None:
    """Фоновая отправка уведомления о покупке подписки админам."""
    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) or not settings.BOT_TOKEN:
        return

    async def _run() -> None:
        from app.database.crud.subscription import get_subscription_by_id
        from app.database.crud.transaction import get_transaction_by_id
        from app.database.crud.user import get_user_by_id
        from app.database.database import AsyncSessionLocal
        from app.services.admin_notification_service import AdminNotificationService

        bot = Bot(token=settings.BOT_TOKEN)
        try:
            async with AsyncSessionLocal() as db:
                user = await get_user_by_id(db, user_id)
                if not user:
                    return
                subscription = await get_subscription_by_id(db, subscription_id)
                if not subscription:
                    return
                transaction = await get_transaction_by_id(db, transaction_id) if transaction_id else None

                service = AdminNotificationService(bot)
                await service.send_subscription_purchase_notification(
                    db=db,
                    user=user,
                    subscription=subscription,
                    transaction=transaction,
                    period_days=period_days,
                    was_trial_conversion=was_trial_conversion,
                    amount_kopeks=amount_kopeks,
                    purchase_type=purchase_type,
                )
        except Exception as e:
            logger.error('Background subscription purchase notification failed', error=e)
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass

    _schedule_bg(_run())


def dispatch_subscription_extension_notification_bg(
    *,
    user_id: int,
    subscription_id: int,
    transaction_id: int,
    extended_days: int,
    old_end_date_iso: str,
    new_end_date_iso: str | None = None,
    balance_after: int | None = None,
) -> None:
    """Фоновая отправка уведомления о продлении подписки админам."""
    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) or not settings.BOT_TOKEN:
        return

    async def _run() -> None:
        from datetime import datetime

        from app.database.crud.subscription import get_subscription_by_id
        from app.database.crud.transaction import get_transaction_by_id
        from app.database.crud.user import get_user_by_id
        from app.database.database import AsyncSessionLocal
        from app.services.admin_notification_service import AdminNotificationService

        bot = Bot(token=settings.BOT_TOKEN)
        try:
            async with AsyncSessionLocal() as db:
                user = await get_user_by_id(db, user_id)
                subscription = await get_subscription_by_id(db, subscription_id)
                transaction = await get_transaction_by_id(db, transaction_id) if transaction_id else None
                if not (user and subscription and transaction):
                    return

                old_end_date = datetime.fromisoformat(old_end_date_iso)
                new_end_date = datetime.fromisoformat(new_end_date_iso) if new_end_date_iso else None

                service = AdminNotificationService(bot)
                await service.send_subscription_extension_notification(
                    db,
                    user,
                    subscription,
                    transaction,
                    extended_days,
                    old_end_date,
                    new_end_date=new_end_date,
                    balance_after=balance_after,
                )
        except Exception as e:
            logger.error('Background subscription extension notification failed', error=e)
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass

    _schedule_bg(_run())


def dispatch_generic_admin_notification_bg(
    handler: Callable[['AdminNotificationService', AsyncSession], Awaitable[None]],  # type: ignore[name-defined]
) -> None:
    """Универсальный фоновый запуск с собственной сессией БД.

    handler получает (service, db) и сам решает что нужно перечитать.
    """
    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) or not settings.BOT_TOKEN:
        return

    async def _run() -> None:
        from app.database.database import AsyncSessionLocal
        from app.services.admin_notification_service import AdminNotificationService

        bot = Bot(token=settings.BOT_TOKEN)
        try:
            async with AsyncSessionLocal() as db:
                service = AdminNotificationService(bot)
                await handler(service, db)
        except Exception as e:
            logger.error('Background admin notification failed', error=e)
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass

    _schedule_bg(_run())
