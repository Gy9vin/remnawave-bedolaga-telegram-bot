"""Админ-меню управления авто-рестартом нод Remnawave."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database.models import User
from app.services.nodes_restart_service import get_state, run_restart_all
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


CB_MENU = 'admin_nodes_restart_menu'
CB_RUN_FORCE = 'admin_nodes_restart_now_force'
CB_RUN_GRACE = 'admin_nodes_restart_now_grace'


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='⚡ Перезапустить СЕЙЧАС (force)', callback_data=CB_RUN_FORCE)],
            [InlineKeyboardButton(text='🐢 Перезапустить СЕЙЧАС (graceful)', callback_data=CB_RUN_GRACE)],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_panel')],
        ]
    )


def _format_dt(dt: datetime | None) -> str:
    if not dt:
        return '—'
    return dt.astimezone(UTC).strftime('%d.%m.%Y %H:%M UTC')


def _build_status_text() -> str:
    enabled = bool(getattr(settings, 'NODES_AUTO_RESTART_ENABLED', False))
    mode = (getattr(settings, 'NODES_AUTO_RESTART_MODE', 'interval') or 'interval').lower()
    interval = int(getattr(settings, 'NODES_AUTO_RESTART_INTERVAL_HOURS', 24) or 24)
    at_hour = int(getattr(settings, 'NODES_AUTO_RESTART_AT_HOUR', 4) or 4)
    force = bool(getattr(settings, 'NODES_AUTO_RESTART_FORCE', True))
    st = get_state()

    lines = [
        '🔄 <b>Авто-перезагрузка нод Remnawave</b>',
        '',
        f'• Статус: {"🟢 включено" if enabled else "🔴 выключено"}',
        f'• Режим: <code>{mode}</code>',
    ]
    if mode == 'daily':
        lines.append(f'• Время рестарта (UTC): <b>{at_hour:02d}:00</b>')
        msk = (at_hour + 3) % 24
        lines.append(f'  (для МСК = <b>{msk:02d}:00</b>)')
    else:
        lines.append(f'• Интервал: <b>{interval}</b> ч')
    lines.append(f'• forceRestart: {"✅ да" if force else "⚪ нет"}')

    lines.append('')
    lines.append(f'• Последний запуск: <i>{_format_dt(st["last_run_at"])}</i>')
    if st['last_result_ok'] is not None:
        lines.append(f'• Результат: {"✅ ok" if st["last_result_ok"] else "❌ ошибка"}')
    if st.get('last_error'):
        lines.append(f'• Ошибка: <code>{st["last_error"][:200]}</code>')

    # Следующий запуск (приблизительно)
    if enabled:
        if mode == 'daily':
            now = datetime.now(UTC)
            target = now.replace(hour=at_hour, minute=0, second=0, microsecond=0)
            if target <= now or st.get('last_daily_fired_date') == now.strftime('%Y-%m-%d'):
                target = target + timedelta(days=1)
            lines.append(f'• Следующий запуск: <i>{_format_dt(target)}</i>')
        elif st['last_run_at']:
            next_at = st['last_run_at'] + timedelta(hours=interval)
            lines.append(f'• Следующий запуск: <i>{_format_dt(next_at)}</i>')

    lines.append('')
    lines.append(
        'Настройки меняются через <b>⚙️ Системные настройки → 🔄 Автоперезагрузка нод</b>.'
    )
    return '\n'.join(lines)


@admin_required
@error_handler
async def show_menu(callback: types.CallbackQuery, db_user: User) -> None:  # noqa: ARG001
    await callback.message.edit_text(
        _build_status_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(),
    )


@admin_required
@error_handler
async def run_now_force(callback: types.CallbackQuery, db_user: User) -> None:
    await callback.message.edit_text(
        '⚡ <b>Запускаю принудительный рестарт всех нод…</b>',
        parse_mode=ParseMode.HTML,
    )
    ok = await run_restart_all(force=True, reason=f'manual_force admin#{db_user.id}')
    suffix = '✅ Команда принята панелью' if ok else '❌ Не удалось — смотри логи бота'
    logger.info('Админ: ручной рестарт нод (force)', admin_id=db_user.id, ok=ok)
    await callback.message.edit_text(
        _build_status_text() + f'\n\n<b>Результат:</b> {suffix}',
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(),
    )


@admin_required
@error_handler
async def run_now_graceful(callback: types.CallbackQuery, db_user: User) -> None:
    await callback.message.edit_text(
        '🐢 <b>Запускаю плавный рестарт всех нод…</b>\n<i>(может не сработать — Remnawave не всегда применяет)</i>',
        parse_mode=ParseMode.HTML,
    )
    ok = await run_restart_all(force=False, reason=f'manual_graceful admin#{db_user.id}')
    suffix = '✅ Команда принята панелью' if ok else '❌ Не удалось — смотри логи бота'
    logger.info('Админ: ручной рестарт нод (graceful)', admin_id=db_user.id, ok=ok)
    await callback.message.edit_text(
        _build_status_text() + f'\n\n<b>Результат:</b> {suffix}',
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_menu, F.data == CB_MENU)
    dp.callback_query.register(run_now_force, F.data == CB_RUN_FORCE)
    dp.callback_query.register(run_now_graceful, F.data == CB_RUN_GRACE)
