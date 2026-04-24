"""Admin handler for managing required channel subscriptions."""

import asyncio
import contextlib

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.crud.required_channel import (
    add_channel,
    delete_channel,
    get_all_channels,
    get_channel_by_id,
    toggle_channel,
    validate_channel_id,
)
from app.database.database import AsyncSessionLocal
from app.services.channel_membership_report_service import (
    ReportAlreadyRunning,
    ReportNotFound,
    channel_membership_report_service,
)
from app.services.channel_subscription_service import channel_subscription_service
from app.utils.decorators import admin_required


logger = structlog.get_logger(__name__)

router = Router(name='admin_required_channels')

# Интервал обновления прогресс-бара в сообщении с отчётом
_REPORT_PROGRESS_INTERVAL = 5.0


class AddChannelStates(StatesGroup):
    waiting_channel_id = State()
    waiting_channel_link = State()
    waiting_channel_title = State()


# -- List channels ----------------------------------------------------------------


def _channels_keyboard(channels: list) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        status = '✅' if ch.is_active else '❌'
        title = ch.title or ch.channel_id
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{status} {title}',
                    callback_data=f'reqch:view:{ch.id}',
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text='➕ Добавить канал', callback_data='reqch:add')])
    buttons.append([InlineKeyboardButton(text='◀️ Назад', callback_data='admin_submenu_settings')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _channel_detail_keyboard(channel_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = '❌ Отключить' if is_active else '✅ Включить'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📊 Отчёт: кто не в канале', callback_data=f'reqch:report:{channel_id}')],
            [InlineKeyboardButton(text=toggle_text, callback_data=f'reqch:toggle:{channel_id}')],
            [InlineKeyboardButton(text='🗑 Удалить', callback_data=f'reqch:delete:{channel_id}')],
            [InlineKeyboardButton(text='◀️ К списку', callback_data='reqch:list')],
        ]
    )


def _format_progress_text(progress: dict) -> str:
    status_emoji = {
        'pending': '⏳',
        'running': '⏳',
        'completed': '✅',
        'failed': '❌',
        'cancelled': '🚫',
    }.get(progress['status'], '❔')
    status_label = {
        'pending': 'Инициализация',
        'running': 'Выполняется',
        'completed': 'Завершён',
        'failed': 'Ошибка',
        'cancelled': 'Отменён',
    }.get(progress['status'], progress['status'])

    total = progress.get('total') or 0
    processed = progress.get('processed') or 0
    pct = f'{(processed / total * 100):.1f}%' if total else '—'

    lines = [
        f'<b>📊 Отчёт по каналу</b> {status_emoji} {status_label}',
        f'Канал: <code>{progress.get("channel_id", "")}</code>',
    ]
    if progress.get('channel_title'):
        lines.append(f'Название: {progress["channel_title"]}')
    lines.extend(
        [
            '',
            f'Всего подписчиков: <b>{total}</b>',
            f'Проверено: <b>{processed}</b> ({pct})',
            f'✅ В канале: <b>{progress.get("in_channel", 0)}</b>',
            f'❌ НЕ в канале: <b>{progress.get("not_in_channel", 0)}</b>',
        ]
    )
    if progress.get('error_message'):
        lines.append(f'\n⚠️ Ошибка: <code>{progress["error_message"]}</code>')
    return '\n'.join(lines)


def _report_progress_keyboard(channel_db_id: int, report_id: str, is_running: bool) -> InlineKeyboardMarkup:
    rows = []
    if is_running:
        rows.append([InlineKeyboardButton(text='🚫 Отменить', callback_data=f'reqch:report_cancel:{report_id}')])
    rows.append([InlineKeyboardButton(text='🔄 Обновить', callback_data=f'reqch:report_status:{report_id}')])
    rows.append([InlineKeyboardButton(text='◀️ К каналу', callback_data=f'reqch:view:{channel_db_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _report_done_keyboard(channel_db_id: int, report_id: str, has_csv: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_csv:
        rows.append([InlineKeyboardButton(text='📥 Скачать CSV', callback_data=f'reqch:report_csv:{report_id}')])
    rows.append([InlineKeyboardButton(text='◀️ К каналу', callback_data=f'reqch:view:{channel_db_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == 'reqch:list')
@admin_required
async def show_channels_list(callback: CallbackQuery, **kwargs) -> None:
    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)

    if not channels:
        text = '<b>📢 Обязательные каналы</b>\n\nКаналы не настроены. Нажмите «Добавить» чтобы создать.'
    else:
        lines = ['<b>📢 Обязательные каналы</b>\n']
        for ch in channels:
            status = '✅' if ch.is_active else '❌'
            title = ch.title or ch.channel_id
            lines.append(f'{status} <code>{ch.channel_id}</code> — {title}')
        text = '\n'.join(lines)

    await callback.message.edit_text(text, reply_markup=_channels_keyboard(channels))
    await callback.answer()


@router.callback_query(F.data.startswith('reqch:view:'))
@admin_required
async def view_channel(callback: CallbackQuery, **kwargs) -> None:
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer('Неверный ID канала', show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ch = await get_channel_by_id(db, channel_db_id)

    if not ch:
        await callback.answer('Канал не найден', show_alert=True)
        return

    status = '✅ Активен' if ch.is_active else '❌ Отключён'
    text = (
        f'<b>{ch.title or "Без названия"}</b>\n\n'
        f'<b>ID:</b> <code>{ch.channel_id}</code>\n'
        f'<b>Ссылка:</b> {ch.channel_link or "—"}\n'
        f'<b>Статус:</b> {status}\n'
        f'<b>Порядок:</b> {ch.sort_order}'
    )

    await callback.message.edit_text(text, reply_markup=_channel_detail_keyboard(ch.id, ch.is_active))
    await callback.answer()


# -- Toggle / Delete ---------------------------------------------------------------


@router.callback_query(F.data.startswith('reqch:toggle:'))
@admin_required
async def toggle_channel_handler(callback: CallbackQuery, **kwargs) -> None:
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer('Неверный ID канала', show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ch = await toggle_channel(db, channel_db_id)

    if ch:
        await channel_subscription_service.invalidate_channels_cache()
        status = 'включён' if ch.is_active else 'отключён'
        await callback.answer(f'Канал {status}', show_alert=True)

    # Refresh list
    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)
    await callback.message.edit_text(
        '<b>📢 Обязательные каналы</b>',
        reply_markup=_channels_keyboard(channels),
    )


@router.callback_query(F.data.startswith('reqch:delete:'))
@admin_required
async def delete_channel_handler(callback: CallbackQuery, **kwargs) -> None:
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer('Неверный ID канала', show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ok = await delete_channel(db, channel_db_id)

    if ok:
        await channel_subscription_service.invalidate_channels_cache()
        await callback.answer('Канал удалён', show_alert=True)
    else:
        await callback.answer('Ошибка удаления', show_alert=True)

    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)
    await callback.message.edit_text(
        '<b>📢 Обязательные каналы</b>',
        reply_markup=_channels_keyboard(channels),
    )


# -- Add channel flow --------------------------------------------------------------


@router.callback_query(F.data == 'reqch:add')
@admin_required
async def start_add_channel(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    await state.set_state(AddChannelStates.waiting_channel_id)
    await callback.message.edit_text(
        '<b>➕ Добавить канал</b>\n\n'
        'Отправьте числовой ID канала (например <code>1234567890</code>).\n'
        'Префикс <code>-100</code> добавляется автоматически.'
    )
    await callback.answer()


@router.message(AddChannelStates.waiting_channel_id)
@admin_required
async def process_channel_id(message: Message, state: FSMContext, **kwargs) -> None:
    if not message.text:
        await message.answer('Отправьте текстовое сообщение.')
        return
    channel_id = message.text.strip()

    # Validate and normalize channel_id (auto-prefixes -100 for bare digits)
    try:
        channel_id = validate_channel_id(channel_id)
    except ValueError as e:
        await message.answer(f'Неверный формат. {e}\n\nПопробуйте ещё раз:')
        return

    await state.update_data(channel_id=channel_id)
    await state.set_state(AddChannelStates.waiting_channel_link)
    await message.answer(
        f'Канал: <code>{channel_id}</code>\n\n'
        'Теперь отправьте ссылку на канал (например <code>https://t.me/mychannel</code>)\n'
        'Или отправьте <code>-</code> чтобы пропустить:'
    )


@router.message(AddChannelStates.waiting_channel_link)
@admin_required
async def process_channel_link(message: Message, state: FSMContext, **kwargs) -> None:
    if not message.text:
        await message.answer('Отправьте текстовое сообщение.')
        return
    link = message.text.strip()
    if link == '-':
        link = None

    if link is not None:
        # Validate and normalize channel link
        if not link.startswith(('https://t.me/', 'http://t.me/', '@')):
            await message.answer('Ссылка должна быть URL вида t.me или @username. Попробуйте ещё раз:')
            return
        if link.startswith('@'):
            link = f'https://t.me/{link[1:]}'
        if link.startswith('http://'):
            link = link.replace('http://', 'https://', 1)

    await state.update_data(channel_link=link)
    await state.set_state(AddChannelStates.waiting_channel_title)
    await message.answer(
        'Отправьте название канала (например <code>Новости проекта</code>)\n'
        'Или отправьте <code>-</code> чтобы пропустить:'
    )


@router.message(AddChannelStates.waiting_channel_title)
@admin_required
async def process_channel_title(message: Message, state: FSMContext, **kwargs) -> None:
    if not message.text:
        await message.answer('Отправьте текстовое сообщение.')
        return
    title = message.text.strip()
    if title == '-':
        title = None

    data = await state.get_data()
    await state.clear()

    async with AsyncSessionLocal() as db:
        try:
            ch = await add_channel(
                db,
                channel_id=data['channel_id'],
                channel_link=data.get('channel_link'),
                title=title,
            )
            await channel_subscription_service.invalidate_channels_cache()

            text = (
                '✅ Канал добавлен!\n\n'
                f'<b>ID:</b> <code>{ch.channel_id}</code>\n'
                f'<b>Ссылка:</b> {ch.channel_link or "—"}\n'
                f'<b>Название:</b> {ch.title or "—"}'
            )
        except Exception as e:
            text = '❌ Ошибка добавления канала. Попробуйте ещё раз.'
            logger.error('Error adding channel', error=e)

    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)

    await message.answer(text, reply_markup=_channels_keyboard(channels))


# -- Report flow ------------------------------------------------------------------


async def _refresh_report_message(
    callback: CallbackQuery,
    channel_db_id: int,
    report_id: str,
) -> bool:
    """Обновляет сообщение с прогрессом. Возвращает True если отчёт ещё идёт."""
    try:
        progress = channel_membership_report_service.get_status(report_id)
    except ReportNotFound:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(
                '⚠️ Отчёт не найден (возможно, просрочен).',
                reply_markup=_channel_detail_keyboard(channel_db_id, True),
            )
        return False

    text = _format_progress_text(progress)
    is_running = progress['status'] in ('pending', 'running')
    if is_running:
        keyboard = _report_progress_keyboard(channel_db_id, report_id, is_running=True)
    else:
        keyboard = _report_done_keyboard(channel_db_id, report_id, has_csv=progress.get('has_csv', False))

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            logger.warning('Не удалось обновить сообщение с прогрессом', error=str(exc))
    return is_running


async def _auto_refresh_loop(callback: CallbackQuery, channel_db_id: int, report_id: str) -> None:
    """Фоновая корутина: обновляет сообщение с прогрессом пока отчёт идёт."""
    try:
        while True:
            await asyncio.sleep(_REPORT_PROGRESS_INTERVAL)
            still_running = await _refresh_report_message(callback, channel_db_id, report_id)
            if not still_running:
                break
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('Ошибка в auto-refresh отчёта', report_id=report_id)


@router.callback_query(F.data.startswith('reqch:report:'))
@admin_required
async def start_report_handler(callback: CallbackQuery, **kwargs) -> None:
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer('Неверный ID канала', show_alert=True)
        return

    async with AsyncSessionLocal() as db:
        ch = await get_channel_by_id(db, channel_db_id)
    if not ch:
        await callback.answer('Канал не найден', show_alert=True)
        return

    try:
        report_id = await channel_membership_report_service.start_report(
            channel_db_id=channel_db_id,
            admin_telegram_id=callback.from_user.id,
        )
    except ReportAlreadyRunning as exc:
        await callback.answer(f'Уже выполняется другой отчёт. {exc}', show_alert=True)
        return
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    progress = channel_membership_report_service.get_status(report_id)
    text = _format_progress_text(progress)
    await callback.message.edit_text(
        text,
        reply_markup=_report_progress_keyboard(channel_db_id, report_id, is_running=True),
    )
    await callback.answer('Отчёт запущен')

    asyncio.create_task(
        _auto_refresh_loop(callback, channel_db_id, report_id),
        name=f'report-refresh-{report_id}',
    )


@router.callback_query(F.data.startswith('reqch:report_status:'))
@admin_required
async def refresh_report_handler(callback: CallbackQuery, **kwargs) -> None:
    try:
        report_id = callback.data.split(':')[2]
    except IndexError:
        await callback.answer('Неверный ID отчёта', show_alert=True)
        return

    try:
        progress = channel_membership_report_service.get_status(report_id)
    except ReportNotFound:
        await callback.answer('Отчёт не найден', show_alert=True)
        return

    await _refresh_report_message(callback, progress['channel_db_id'], report_id)
    await callback.answer()


@router.callback_query(F.data.startswith('reqch:report_cancel:'))
@admin_required
async def cancel_report_handler(callback: CallbackQuery, **kwargs) -> None:
    try:
        report_id = callback.data.split(':')[2]
    except IndexError:
        await callback.answer('Неверный ID отчёта', show_alert=True)
        return

    try:
        await channel_membership_report_service.cancel(report_id)
    except ReportNotFound:
        await callback.answer('Отчёт не найден', show_alert=True)
        return

    await callback.answer('Отмена запрошена')


@router.callback_query(F.data.startswith('reqch:report_csv:'))
@admin_required
async def download_report_csv_handler(callback: CallbackQuery, **kwargs) -> None:
    try:
        report_id = callback.data.split(':')[2]
    except IndexError:
        await callback.answer('Неверный ID отчёта', show_alert=True)
        return

    try:
        csv_bytes, filename = channel_membership_report_service.get_csv(report_id)
    except ReportNotFound:
        await callback.answer('Отчёт не найден', show_alert=True)
        return
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    try:
        progress = channel_membership_report_service.get_status(report_id)
    except ReportNotFound:
        progress = {}

    caption = '📥 Список подписчиков, которых нет в канале'
    if progress.get('channel_title'):
        caption += f'\nКанал: {progress["channel_title"]}'

    await callback.message.answer_document(
        document=BufferedInputFile(csv_bytes, filename=filename),
        caption=caption,
    )
    await callback.answer('CSV отправлен')


def register_handlers(dp_router: Router) -> None:
    dp_router.include_router(router)
