"""Telegram admin button: trigger the Google-sunset set-password invite campaign."""

import structlog
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.crud.user import get_google_migration_stats
from app.database.database import AsyncSessionLocal
from app.services.google_migration_service import google_migration_service
from app.utils.decorators import admin_required, error_handler

logger = structlog.get_logger(__name__)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Отправить всем', callback_data='admin_google_migration_send')],
            [InlineKeyboardButton(text='◀️ Назад', callback_data='admin_messages')],
        ]
    )


@admin_required
@error_handler
async def show_menu(callback: CallbackQuery) -> None:
    async with AsyncSessionLocal() as session:
        stats = await get_google_migration_stats(session)
    status = google_migration_service.get_status()
    at_risk = stats['total'] - stats['with_password']
    text = (
        '📧 <b>Миграция Google-пользователей</b>\n\n'
        f'Всего с Google: <b>{stats["total"]}</b>\n'
        f'Только через Google: <b>{stats["google_only"]}</b>\n'
        f'Уже задали пароль: <b>{stats["with_password"]}</b>\n'
        f'❗️ Не задали пароль: <b>{at_risk}</b>\n\n'
    )
    if status['running']:
        text += f'⏳ Идёт рассылка: {status["sent"]}/{status["total"]} (ошибок {status["failed"]})'
    elif status['finished_at']:
        text += f'✅ Последняя рассылка: отправлено {status["sent"]}, ошибок {status["failed"]}'
    text += '\n\nНажмите кнопку — всем этим пользователям уйдёт письмо с долгоживущей ссылкой на задание пароля.'
    await callback.message.edit_text(text, reply_markup=_confirm_keyboard())
    await callback.answer()


@admin_required
@error_handler
async def handle_send_invites(callback: CallbackQuery) -> None:
    started = await google_migration_service.start()
    if started:
        await callback.answer('Рассылка запущена ✅', show_alert=True)
    else:
        await callback.answer('Рассылка уже идёт ⏳', show_alert=True)


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_menu, F.data == 'admin_google_migration')
    dp.callback_query.register(handle_send_invites, F.data == 'admin_google_migration_send')
