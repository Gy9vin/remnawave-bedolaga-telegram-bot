"""Админ-меню управления fallback-сквадом из бота.

Сейчас содержит только одну операцию — массовый перевод просроченных
подписок в fallback-сквад. Та же логика, что и кнопка «Прогнать expired
в fallback» в кабинете (`/admin/expiry-fallback`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus, Tariff, User
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


CALLBACK_MENU = 'admin_expiry_fallback_menu'
CALLBACK_CONFIRM = 'admin_expiry_fallback_confirm'
CALLBACK_RUN = 'admin_expiry_fallback_run'
CALLBACK_RESTORE_CONFIRM = 'admin_expiry_fallback_restore_confirm'
CALLBACK_RESTORE_RUN = 'admin_expiry_fallback_restore_run'
CALLBACK_REGRACE_CONFIRM = 'admin_expiry_fallback_regrace_confirm'
CALLBACK_REGRACE_RUN = 'admin_expiry_fallback_regrace_run'


def _menu_keyboard(enabled: bool, has_uuid: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if enabled and has_uuid:
        rows.append(
            [
                InlineKeyboardButton(
                    text='🚀 Прогнать expired в fallback',
                    callback_data=CALLBACK_CONFIRM,
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text='🔧 Вернуть ошибочно загнанных',
                    callback_data=CALLBACK_RESTORE_CONFIRM,
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text='🔁 Вернуть DISABLED → fallback (+3 дня)',
                    callback_data=CALLBACK_REGRACE_CONFIRM,
                )
            ]
        )
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_users')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='✅ Запустить', callback_data=CALLBACK_RUN),
                InlineKeyboardButton(text='⬅️ Отмена', callback_data=CALLBACK_MENU),
            ]
        ]
    )


def _parse_dev_ids() -> list[str]:
    raw_ids = getattr(settings, 'EXPIRY_FALLBACK_DEV_USER_IDS', None) or ''
    if isinstance(raw_ids, str):
        return [x.strip() for x in raw_ids.split(',') if x.strip()]
    return [str(x).strip() for x in (raw_ids or [])]


async def _diagnose_whitelist(dev_ids: list[str]) -> list[str]:
    """Для каждого ID из whitelist показать, попадёт ли он в fallback при scan.

    Возвращает список форматированных строк (по одной на проверенный ID).
    """
    if not dev_ids:
        return []

    int_ids: list[int] = []
    for raw in dev_ids:
        try:
            int_ids.append(int(raw))
        except ValueError:
            continue

    if not int_ids:
        return ['• <i>Не удалось распарсить ни один ID как число</i>']

    lines: list[str] = []
    async with AsyncSessionLocal() as db:
        for raw_id in int_ids:
            result = await db.execute(
                select(User).where(or_(User.id == raw_id, User.telegram_id == raw_id))
            )
            users = list(result.scalars().all())

            if not users:
                lines.append(
                    f'❌ <code>{raw_id}</code> — не найден ни как DB ID, ни как TG ID'
                )
                continue

            for user in users:
                match_kind = 'DB id' if user.id == raw_id else 'TG id'
                if match_kind == 'TG id':
                    lines.append(
                        f'⚠️ <code>{raw_id}</code> — это <b>TG id</b> юзера <b>{user.id}</b>! '
                        f'В whitelist нужно класть DB id (<code>{user.id}</code>).'
                    )

                subs_q = await db.execute(
                    select(Subscription)
                    .options(selectinload(Subscription.tariff))
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.id.desc())
                )
                subs = list(subs_q.scalars().all())

                if not subs:
                    lines.append(f'   └ Подписок нет — нечего переводить.')
                    continue

                for sub in subs[:3]:
                    status_emoji = {'active': '🟢', 'expired': '⚪', 'trial': '🎁'}.get(
                        sub.status, '⚫'
                    )
                    end_local = (
                        sub.end_date.astimezone(UTC).strftime('%d.%m %H:%M')
                        if sub.end_date
                        else '—'
                    )

                    flags = []
                    if sub.expiry_fallback_active:
                        flags.append('уже в EXPIRY-fallback')
                    if sub.traffic_fallback_active:
                        flags.append('уже в TRAFFIC-fallback')
                    if not sub.remnawave_uuid:
                        flags.append('БЕЗ remnawave_uuid')
                    is_daily = bool(getattr(sub.tariff, 'is_daily', False))
                    if is_daily and not getattr(sub, 'is_daily_paused', False):
                        flags.append('daily-тариф')

                    now = datetime.now(UTC)
                    end_dt = sub.end_date
                    if end_dt is not None and end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=UTC)
                    expired_by_date = bool(end_dt and end_dt <= now)

                    will_be_picked = (
                        sub.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.EXPIRED.value)
                        and expired_by_date
                        and not sub.expiry_fallback_active
                        and not sub.traffic_fallback_active
                        and bool(sub.remnawave_uuid)
                        and not (is_daily and not getattr(sub, 'is_daily_paused', False))
                    )

                    pick = '✅ попадёт в fallback' if will_be_picked else '❌ НЕ попадёт'
                    if not will_be_picked:
                        if not expired_by_date:
                            pick += ' (end_date в будущем)'
                        elif sub.status not in (
                            SubscriptionStatus.ACTIVE.value,
                            SubscriptionStatus.EXPIRED.value,
                        ):
                            pick += f' (status={sub.status})'
                        elif flags:
                            pick += f' ({", ".join(flags)})'

                    lines.append(
                        f'   └ Sub #{sub.id} {status_emoji} {sub.status}, '
                        f'end {end_local} → {pick}'
                    )
    return lines


async def _build_status_text() -> str:
    enabled = bool(getattr(settings, 'EXPIRY_FALLBACK_ENABLED', False))
    uuid = getattr(settings, 'EXPIRY_FALLBACK_SQUAD_UUID', None)
    dev_mode = bool(getattr(settings, 'EXPIRY_FALLBACK_DEV_MODE', False))
    dev_ids = _parse_dev_ids()

    lines = [
        '🛟 <b>Fallback-сквад при истечении</b>',
        '',
        f'• Система: {"🟢 включена" if enabled else "🔴 выключена"}',
        f'• Сквад: <code>{uuid}</code>' if uuid else '• Сквад: <i>не задан</i>',
        f'• DEV_MODE: {"🟢 включён" if dev_mode else "⚪ выключен"}',
    ]
    if dev_mode:
        if dev_ids:
            preview = ', '.join(dev_ids[:5])
            if len(dev_ids) > 5:
                preview += f' (+{len(dev_ids) - 5})'
            lines.append(f'• Whitelist user_id: <code>{preview}</code>')
        else:
            lines.append('• Whitelist user_id: <i>пусто</i>')

        diag = await _diagnose_whitelist(dev_ids)
        if diag:
            lines.append('')
            lines.append('<b>Диагностика whitelist:</b>')
            lines.extend(diag)

    lines.append('')
    lines.append(
        'Кнопка ниже сканирует БД и переводит в fallback все подписки '
        'с истёкшим сроком. Если включён DEV_MODE — только юзеров из whitelist.'
    )
    return '\n'.join(lines)


@admin_required
@error_handler
async def show_menu(callback: types.CallbackQuery, db_user: User) -> None:  # noqa: ARG001
    enabled = bool(getattr(settings, 'EXPIRY_FALLBACK_ENABLED', False))
    has_uuid = bool(getattr(settings, 'EXPIRY_FALLBACK_SQUAD_UUID', None))
    text = await _build_status_text()
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(enabled, has_uuid),
    )


@admin_required
@error_handler
async def confirm_scan(callback: types.CallbackQuery, db_user: User) -> None:  # noqa: ARG001
    dev_mode = bool(getattr(settings, 'EXPIRY_FALLBACK_DEV_MODE', False))
    if dev_mode:
        warn = (
            'Включён <b>DEV_MODE</b> — переведу только юзеров из '
            '<code>EXPIRY_FALLBACK_DEV_USER_IDS</code>.'
        )
    else:
        warn = (
            '<b>DEV_MODE выключен</b> — будут переведены <b>ВСЕ</b> юзеры '
            'с истёкшей подпиской. Это массовая операция!'
        )
    await callback.message.edit_text(
        f'⚠️ <b>Подтверждение</b>\n\n{warn}\n\nПродолжить?',
        parse_mode=ParseMode.HTML,
        reply_markup=_confirm_keyboard(),
    )


@admin_required
@error_handler
async def run_scan(callback: types.CallbackQuery, db_user: User) -> None:
    from app.services.expiry_fallback_service import scan_and_move_expired

    await callback.message.edit_text(
        '🔄 <b>Сканирую базу…</b>\n\nПодождите, операция может занять до минуты.',
        parse_mode=ParseMode.HTML,
    )

    async with AsyncSessionLocal() as db:
        stats = await scan_and_move_expired(db)

    if not stats.get('success'):
        await callback.message.edit_text(
            f'❌ <b>Не удалось запустить</b>\n\n{stats.get("error", "Неизвестная ошибка")}',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='⬅️ Назад', callback_data=CALLBACK_MENU)]
                ]
            ),
        )
        return

    dev_active = stats.get('dev_mode_active', False)
    text = (
        '✅ <b>Готово</b>\n\n'
        f'• Просканировано: <b>{stats["scanned"]}</b>\n'
        f'• Переведено в fallback: <b>{stats["moved"]}</b>\n'
        f'• Пропущено (DEV-whitelist): <b>{stats["skipped_dev_mode"]}</b>\n'
        f'• Без remnawave_uuid: <b>{stats["skipped_no_remnawave_uuid"]}</b>\n'
        f'• Ошибок: <b>{stats["failed"]}</b>\n\n'
        f'DEV_MODE: {"🟢 включён" if dev_active else "⚪ выключен"}'
    )
    logger.info(
        'Бот: scan_and_move_expired',
        admin_telegram_id=db_user.telegram_id,
        admin_user_id=db_user.id,
        stats=stats,
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='⬅️ Назад', callback_data=CALLBACK_MENU)]
            ]
        ),
    )


@admin_required
@error_handler
async def confirm_restore(callback: types.CallbackQuery, db_user: User) -> None:  # noqa: ARG001
    text = (
        '⚠️ <b>Вернуть ошибочно загнанных из fallback</b>\n\n'
        'Просканирует подписки в fallback-скваде. Если в БД '
        '<code>status=ACTIVE</code> и <code>end_date</code> в будущем (юзер '
        'реально продлился), вернёт его в исходный сквад.\n\n'
        'Безопасно — настоящих просроченных не трогает. Продолжить?'
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text='✅ Запустить', callback_data=CALLBACK_RESTORE_RUN),
                    InlineKeyboardButton(text='⬅️ Отмена', callback_data=CALLBACK_MENU),
                ]
            ]
        ),
    )


@admin_required
@error_handler
async def run_restore(callback: types.CallbackQuery, db_user: User) -> None:
    from app.services.expiry_fallback_service import scan_and_restore_active

    await callback.message.edit_text(
        '🔄 <b>Сканирую fallback на ошибочные…</b>\n\nОперация может занять до минуты.',
        parse_mode=ParseMode.HTML,
    )

    async with AsyncSessionLocal() as db:
        stats = await scan_and_restore_active(db)

    if not stats.get('success'):
        await callback.message.edit_text(
            f'❌ <b>Не удалось запустить</b>\n\n{stats.get("error", "Неизвестная ошибка")}',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='⬅️ Назад', callback_data=CALLBACK_MENU)]
                ]
            ),
        )
        return

    text = (
        '✅ <b>Готово</b>\n\n'
        f'• Просканировано: <b>{stats["scanned"]}</b>\n'
        f'• Возвращено в исходный сквад: <b>{stats["restored"]}</b>\n'
        f'• Пропущено (реально в fallback): <b>{stats["skipped_genuine_fallback"]}</b>\n'
        f'• Ошибок: <b>{stats["failed"]}</b>\n'
    )
    logger.info(
        'Бот: scan_and_restore_active',
        admin_telegram_id=db_user.telegram_id,
        admin_user_id=db_user.id,
        stats=stats,
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='⬅️ Назад', callback_data=CALLBACK_MENU)]
            ]
        ),
    )


@admin_required
@error_handler
async def confirm_regrace(callback: types.CallbackQuery, db_user: User) -> None:  # noqa: ARG001
    grace_days = int(getattr(settings, 'EXPIRY_FALLBACK_GRACE_DAYS', 3) or 3)
    text = (
        '⚠️ <b>Вернуть всех DISABLED в fallback</b>\n\n'
        f'Подписки со статусом <code>DISABLED</code>, истёкшие, не триальные, '
        f'у пользователей не в BLOCKED — будут восстановлены в fallback-сквад '
        f'с новым grace-периодом <b>{grace_days} дн.</b>\n\n'
        f'• enable_user() в Remnawave\n'
        f'• status → EXPIRED\n'
        f'• move_to_fallback(reason="expired")\n\n'
        f'Юзеры получат шанс продлиться. Cleanup сработает снова через '
        f'{int(getattr(settings, "EXPIRY_FALLBACK_DAYS", 3) or 3)} дн., '
        f'если не продлят.\n\n'
        f'Продолжить?'
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text='✅ Запустить', callback_data=CALLBACK_REGRACE_RUN),
                    InlineKeyboardButton(text='⬅️ Отмена', callback_data=CALLBACK_MENU),
                ]
            ]
        ),
    )


@admin_required
@error_handler
async def run_regrace(callback: types.CallbackQuery, db_user: User) -> None:
    from app.services.expiry_fallback_service import regrace_disabled_subscriptions

    await callback.message.edit_text(
        '🔄 <b>Возвращаю DISABLED в fallback…</b>\n\nОперация может занять несколько минут.',
        parse_mode=ParseMode.HTML,
    )

    async with AsyncSessionLocal() as db:
        stats = await regrace_disabled_subscriptions(db)

    if not stats.get('success'):
        await callback.message.edit_text(
            f'❌ <b>Не удалось запустить</b>\n\n{stats.get("error", "Неизвестная ошибка")}',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='⬅️ Назад', callback_data=CALLBACK_MENU)]
                ]
            ),
        )
        return

    text = (
        '✅ <b>Готово</b>\n\n'
        f'• Найдено DISABLED: <b>{stats["scanned"]}</b>\n'
        f'• Возвращено в fallback: <b>{stats["restored"]}</b>\n'
        f'• Пропущено (BLOCKED юзер): <b>{stats["skipped_blocked_user"]}</b>\n'
        f'• Пропущено (нет UUID): <b>{stats["skipped_no_uuid"]}</b>\n'
        f'• Ошибок: <b>{stats["failed"]}</b>\n'
    )
    logger.info(
        'Бот: regrace_disabled_subscriptions',
        admin_telegram_id=db_user.telegram_id,
        admin_user_id=db_user.id,
        stats=stats,
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='⬅️ Назад', callback_data=CALLBACK_MENU)]
            ]
        ),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_menu, F.data == CALLBACK_MENU)
    dp.callback_query.register(confirm_scan, F.data == CALLBACK_CONFIRM)
    dp.callback_query.register(run_scan, F.data == CALLBACK_RUN)
    dp.callback_query.register(confirm_restore, F.data == CALLBACK_RESTORE_CONFIRM)
    dp.callback_query.register(run_restore, F.data == CALLBACK_RESTORE_RUN)
    dp.callback_query.register(confirm_regrace, F.data == CALLBACK_REGRACE_CONFIRM)
    dp.callback_query.register(run_regrace, F.data == CALLBACK_REGRACE_RUN)
