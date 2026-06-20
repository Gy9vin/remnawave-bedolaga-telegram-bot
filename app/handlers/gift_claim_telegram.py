"""Handlers for gift subscription Accept / Decline callbacks (Telegram DM invite flow)."""

import html as html_mod

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InaccessibleMessage
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.database import AsyncSessionLocal
from app.database.models import GuestPurchase, GuestPurchaseStatus


logger = structlog.get_logger(__name__)

_GIFT_NOT_FOUND = 'Подарок не найден или недоступен.'
_ALREADY_ACTIVATED = '✅ Подарок уже активирован.'
_NOT_FOR_YOU = 'Это приглашение предназначено не для вас.'
_SELF_ACCEPT = 'Нельзя принять собственный подарок.'


async def handle_gift_accept(callback: types.CallbackQuery) -> None:
    """Handle gift_accept:{purchase_id} — verify identity then fulfill the purchase."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer('Сообщение устарело. Попробуйте /start.', show_alert=True)
        return

    if not callback.data:
        return

    parts = callback.data.split(':', 1)
    if len(parts) != 2:
        await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
        return

    try:
        purchase_id = int(parts[1])
    except ValueError:
        await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
        return

    # Lazy imports to avoid circular imports (same pattern as rest of codebase)
    from app.services.guest_purchase_service import (
        GuestPurchaseError,
        fulfill_purchase,
        resolve_existing_telegram_user,
    )

    # ── Phase 1: load purchase scalars (fast DB-only, no Bot API) ───────────
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GuestPurchase)
            .options(selectinload(GuestPurchase.buyer), selectinload(GuestPurchase.tariff))
            .where(GuestPurchase.id == purchase_id)
        )
        purchase = result.scalars().first()

        if not purchase or not purchase.is_gift:
            await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
            return

        # Idempotency: already delivered
        if purchase.status == GuestPurchaseStatus.DELIVERED.value:
            await callback.answer(_ALREADY_ACTIVATED, show_alert=True)
            if not isinstance(callback.message, InaccessibleMessage):
                await callback.message.edit_text('✅ Подарок уже активирован.', parse_mode=None)
            return

        if purchase.status != GuestPurchaseStatus.PAID.value:
            await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
            return

        if not purchase.gift_recipient_value:
            await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
            return

        # Capture all scalars before session closes
        gift_recipient_value = purchase.gift_recipient_value
        purchase_token = purchase.token
        period_days = purchase.period_days
        buyer_telegram_id = purchase.buyer.telegram_id if purchase.buyer else None
        tariff_name = html_mod.escape(purchase.tariff.name) if purchase.tariff and purchase.tariff.name else ''

    # ── Phase 2: resolve recipient — Bot API call here, NO DB connection held ──
    async with AsyncSessionLocal() as resolve_db:
        resolved = await resolve_existing_telegram_user(resolve_db, gift_recipient_value)
        resolved_tg_id = resolved.telegram_id if resolved else None

    # Identity check: caller must be the intended recipient
    if resolved_tg_id != callback.from_user.id:
        await callback.answer(_NOT_FOR_YOU, show_alert=True)
        return

    # Anti-self check (only reached by confirmed recipients)
    if buyer_telegram_id and buyer_telegram_id == callback.from_user.id:
        await callback.answer(_SELF_ACCEPT, show_alert=True)
        return

    # All checks passed — answer quickly, edit message, then fulfill
    await callback.answer()
    if not isinstance(callback.message, InaccessibleMessage):
        await callback.message.edit_text('⏳ Активируем подарок...', parse_mode=None)

    # ── Phase 3: fulfill in its own session ─────────────────────────────────
    async with AsyncSessionLocal() as db:
        try:
            await fulfill_purchase(db, purchase_token, pre_resolved_telegram_id=callback.from_user.id)
        except GuestPurchaseError as exc:
            logger.warning(
                'Gift accept via DM callback failed',
                purchase_id=purchase_id,
                telegram_id=callback.from_user.id,
                error=exc.message,
            )
            if not isinstance(callback.message, InaccessibleMessage):
                if exc.status_code >= 500:
                    await callback.message.edit_text(
                        'Произошла ошибка при активации. Попробуйте позже.', parse_mode=None
                    )
                else:
                    await callback.message.edit_text(
                        f'Не удалось активировать подарок: {html_mod.escape(exc.message)}',
                        parse_mode=None,
                    )
            return
        except Exception:
            logger.exception(
                'Unexpected error during gift DM accept',
                purchase_id=purchase_id,
                telegram_id=callback.from_user.id,
            )
            if not isinstance(callback.message, InaccessibleMessage):
                await callback.message.edit_text(
                    'Произошла ошибка при активации. Попробуйте позже.', parse_mode=None
                )
            return

    period_text = f'{period_days} дн.' if period_days else ''
    tariff_text = f'{tariff_name} — {period_text}' if tariff_name else period_text

    if not isinstance(callback.message, InaccessibleMessage):
        await callback.message.edit_text(
            f'✅ <b>Подарок активирован!</b>\n{tariff_text}\n\nВаша подписка обновлена.',
        )


async def handle_gift_decline(callback: types.CallbackQuery) -> None:
    """Handle gift_decline:{purchase_id} — leave purchase as PAID, remove buttons."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer('Сообщение устарело.', show_alert=True)
        return

    if not callback.data:
        return

    parts = callback.data.split(':', 1)
    if len(parts) != 2:
        await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
        return

    try:
        purchase_id = int(parts[1])
    except ValueError:
        await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
        return

    # Lazy imports to avoid circular imports (same pattern as rest of codebase)
    from app.services.guest_purchase_service import resolve_existing_telegram_user

    # Verify the purchase exists and check recipient identity
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GuestPurchase).where(GuestPurchase.id == purchase_id)
        )
        purchase = result.scalars().first()

        if not purchase or not purchase.is_gift:
            await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
            return

        gift_recipient_value = purchase.gift_recipient_value

    # Identity check: caller must be the intended recipient
    if gift_recipient_value:
        async with AsyncSessionLocal() as resolve_db:
            resolved = await resolve_existing_telegram_user(resolve_db, gift_recipient_value)
            resolved_tg_id = resolved.telegram_id if resolved else None

        if resolved_tg_id != callback.from_user.id:
            await callback.answer(_NOT_FOR_YOU, show_alert=True)
            return

    await callback.answer()
    if not isinstance(callback.message, InaccessibleMessage):
        await callback.message.edit_text(
            'Вы отклонили подарок. Ссылку на активацию можно запросить у отправителя.',
            parse_mode=None,
        )

    logger.info(
        'Gift DM invite declined',
        purchase_id=purchase_id,
        telegram_id=callback.from_user.id,
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_gift_accept, F.data.startswith('gift_accept:'))
    dp.callback_query.register(handle_gift_decline, F.data.startswith('gift_decline:'))
