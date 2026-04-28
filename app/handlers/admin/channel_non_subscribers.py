"""Хендлеры: список подписчиков с активной подпиской, не состоящих в обязательных каналах."""

import asyncio
import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.channel_report import get_subscribers_not_in_channels
from app.database.crud.required_channel import get_active_channels
from app.database.models import User
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

_PER_PAGE = 15
_STALE_HOURS = 24
_SEND_DELAY = 0.05  # ~20 msg/s


def _format_name(row: dict) -> str:
    if row.get('username'):
        return f"@{row['username']}"
    parts = [row.get('first_name') or '', row.get('last_name') or '']
    return ' '.join(p for p in parts if p) or 'Без имени'


def _build_keyboard(page: int, total_pages: int, total: int) -> InlineKeyboardMarkup:
    buttons = []
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_chan_nonsub_page_{page - 1}'))
        nav.append(InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_chan_nonsub_page_{page + 1}'))
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_chan_nonsub_refresh')])
    buttons.append([
        InlineKeyboardButton(
            text=f'📢 Отправить предупреждение ({total})',
            callback_data='admin_chan_nonsub_warn_confirm',
        )
    ])
    buttons.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_users')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_list(
    callback: types.CallbackQuery,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1,
) -> None:
    data = await state.get_data()
    users = data.get('chan_nonsub_list')
    fetched_at = data.get('chan_nonsub_fetched_at')

    needs_refresh = (
        users is None
        or fetched_at is None
        or datetime.now(UTC) - datetime.fromisoformat(fetched_at) > timedelta(hours=_STALE_HOURS)
    )

    if needs_refresh:
        rows = await get_subscribers_not_in_channels(db)
        users = [
            {
                'user_id': r['user_id'],
                'telegram_id': r['telegram_id'],
                'username': r['username'],
                'first_name': r['first_name'],
                'last_name': r['last_name'],
            }
            for r in rows
        ]
        await state.update_data(
            chan_nonsub_list=users,
            chan_nonsub_fetched_at=datetime.now(UTC).isoformat(),
        )

    total = len(users)
    if total == 0:
        await callback.message.edit_text(
            '✅ <b>Все подписчики с активной подпиской состоят в обязательных каналах.</b>\n\n'
            '<i>Данные из кэша. Для актуальной проверки используй отчёт в кабинете.</i>',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_chan_nonsub_refresh')],
                [InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_users')],
            ]),
        )
        await callback.answer()
        return

    total_pages = (total + _PER_PAGE - 1) // _PER_PAGE
    page = max(1, min(page, total_pages))
    page_users = users[(page - 1) * _PER_PAGE: page * _PER_PAGE]

    lines = [f'🔕 <b>Подписчики не в канале</b> — {total} чел. (стр. {page}/{total_pages})\n']
    for u in page_users:
        name = html.escape(_format_name(u))
        tid = u.get('telegram_id', '?')
        lines.append(f'• {name} — <code>{tid}</code>')

    lines.append('\n<i>⚠️ Данные из кэша. Для живой проверки — отчёт в кабинете.</i>')

    await callback.message.edit_text(
        '\n'.join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_keyboard(page, total_pages, total),
    )
    await callback.answer()


def _build_warning_text(channels: list) -> str:
    """Формирует текст предупреждения с ссылками на каналы."""
    lines = [
        '⚠️ <b>Важное уведомление</b>\n',
        'У вас активная подписка, однако вы не подписаны на наш информационный канал.',
        '',
        'Подписка на канал является обязательным условием использования сервиса.',
        'Пожалуйста, подпишитесь, чтобы продолжить пользоваться VPN.',
    ]

    if channels:
        lines.append('')
        lines.append('📌 <b>Обязательные каналы:</b>')
        for ch in channels:
            title = html.escape(ch.title or ch.channel_id)
            if ch.channel_link:
                lines.append(f'• <a href="{ch.channel_link}">{title}</a>')
            else:
                lines.append(f'• {title}')

    return '\n'.join(lines)


@admin_required
@error_handler
async def show_channel_non_subscribers(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    await _show_list(callback, db, state, page=1)


@admin_required
@error_handler
async def refresh_channel_non_subscribers(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    await state.update_data(chan_nonsub_list=None, chan_nonsub_fetched_at=None)
    await _show_list(callback, db, state, page=1)


@admin_required
@error_handler
async def paginate_channel_non_subscribers(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    page = int(callback.data.split('_')[-1])
    await _show_list(callback, db, state, page=page)


@admin_required
@error_handler
async def confirm_warn(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Показывает превью сообщения и кнопку подтверждения."""
    data = await state.get_data()
    users = data.get('chan_nonsub_list') or []

    if not users:
        await callback.answer('Список пуст, сначала обновите.', show_alert=True)
        return

    channels = await get_active_channels(db)
    warn_text = _build_warning_text(channels)

    preview = (
        f'👥 Получателей: <b>{len(users)}</b>\n\n'
        f'<b>Текст сообщения:</b>\n'
        f'<blockquote>{warn_text}</blockquote>\n\n'
        'Отправить?'
    )

    await callback.message.edit_text(
        preview,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text='✅ Отправить', callback_data='admin_chan_nonsub_warn_send'),
                InlineKeyboardButton(text='❌ Отмена', callback_data='admin_chan_nonsub'),
            ]
        ]),
    )
    await callback.answer()


@admin_required
@error_handler
async def send_warn(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Рассылает предупреждение всем не подписанным на канал подписчикам."""
    data = await state.get_data()
    users = data.get('chan_nonsub_list') or []

    if not users:
        await callback.answer('Список пуст.', show_alert=True)
        return

    channels = await get_active_channels(db)
    warn_text = _build_warning_text(channels)

    total = len(users)
    sent = 0
    failed = 0
    blocked = 0

    await callback.message.edit_text(
        f'📤 <b>Рассылка...</b> 0/{total}',
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()

    last_edit = datetime.now(UTC)

    for i, u in enumerate(users, 1):
        tid = u.get('telegram_id')
        if not tid:
            failed += 1
            continue

        for attempt in range(3):
            try:
                await bot.send_message(
                    chat_id=tid,
                    text=warn_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                blocked += 1
                break
            except TelegramBadRequest as e:
                err = str(e).lower()
                if 'bot was blocked' in err or 'user is deactivated' in err or 'chat not found' in err:
                    blocked += 1
                else:
                    failed += 1
                break
            except Exception:
                if attempt == 2:
                    failed += 1
                else:
                    await asyncio.sleep(0.5)

        await asyncio.sleep(_SEND_DELAY)

        now = datetime.now(UTC)
        if (now - last_edit).total_seconds() >= 3:
            last_edit = now
            try:
                await callback.message.edit_text(
                    f'📤 <b>Рассылка...</b> {i}/{total}\n✅ {sent}  ❌ {failed}  🚫 {blocked}',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    logger.info('Рассылка предупреждения о канале завершена', sent=sent, failed=failed, blocked=blocked)

    await callback.message.edit_text(
        f'✅ <b>Рассылка завершена</b>\n\n'
        f'• Отправлено: {sent}\n'
        f'• Заблокировали бота: {blocked}\n'
        f'• Ошибок: {failed}',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='⬅️ К списку', callback_data='admin_chan_nonsub_refresh')],
        ]),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(
        show_channel_non_subscribers,
        F.data == 'admin_chan_nonsub',
    )
    dp.callback_query.register(
        refresh_channel_non_subscribers,
        F.data == 'admin_chan_nonsub_refresh',
    )
    dp.callback_query.register(
        paginate_channel_non_subscribers,
        F.data.startswith('admin_chan_nonsub_page_'),
    )
    dp.callback_query.register(
        confirm_warn,
        F.data == 'admin_chan_nonsub_warn_confirm',
    )
    dp.callback_query.register(
        send_warn,
        F.data == 'admin_chan_nonsub_warn_send',
    )
