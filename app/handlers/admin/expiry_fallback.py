"""Админ-меню управления fallback-сквадом из бота.

Сейчас содержит только одну операцию — массовый перевод просроченных
подписок в fallback-сквад. Та же логика, что и кнопка «Прогнать expired
в fallback» в кабинете (`/admin/expiry-fallback`).
"""

from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


CALLBACK_MENU = 'admin_expiry_fallback_menu'
CALLBACK_CONFIRM = 'admin_expiry_fallback_confirm'
CALLBACK_RUN = 'admin_expiry_fallback_run'


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


def _build_status_text() -> str:
    enabled = bool(getattr(settings, 'EXPIRY_FALLBACK_ENABLED', False))
    uuid = getattr(settings, 'EXPIRY_FALLBACK_SQUAD_UUID', None)
    dev_mode = bool(getattr(settings, 'EXPIRY_FALLBACK_DEV_MODE', False))
    raw_ids = getattr(settings, 'EXPIRY_FALLBACK_DEV_USER_IDS', None) or ''
    if isinstance(raw_ids, str):
        dev_ids = [x.strip() for x in raw_ids.split(',') if x.strip()]
    else:
        dev_ids = [str(x).strip() for x in (raw_ids or [])]

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
    await callback.message.edit_text(
        _build_status_text(),
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


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_menu, F.data == CALLBACK_MENU)
    dp.callback_query.register(confirm_scan, F.data == CALLBACK_CONFIRM)
    dp.callback_query.register(run_scan, F.data == CALLBACK_RUN)
