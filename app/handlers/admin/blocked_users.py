"""
Хендлеры админ-панели для управления заблокированными пользователями.

Позволяет сканировать пользователей, выявлять тех, кто заблокировал бота,
и выполнять очистку БД и панели Remnawave.
"""

import html
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import User
from app.services.blocked_users_service import (
    BlockCheckResult,
    BlockedUserAction,
    BlockedUsersService,
)
from app.services.penalty_squad_service import penalize_user
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


# =============================================================================
# Enums для текстов и callback_data
# =============================================================================


class BlockedUsersText(Enum):
    """Тексты для сообщений модуля заблокированных пользователей."""

    MENU_TITLE = '🔒 <b>Проверка заблокированных пользователей</b>'
    MENU_DESCRIPTION = (
        '\n\nЗдесь вы можете проверить, какие пользователи заблокировали бота, '
        'и очистить их из базы данных и панели Remnawave.\n\n'
        '<b>Как это работает:</b>\n'
        '1. Сканирование отправляет тестовый запрос каждому пользователю\n'
        '2. Если пользователь заблокировал бота - получаем ошибку\n'
        '3. Можно удалить таких пользователей из БД и/или Remnawave'
    )

    SCAN_STARTED = '🔄 <b>Сканирование запущено...</b>\n\nЭто может занять несколько минут.'
    SCAN_PROGRESS = '🔄 <b>Сканирование:</b> {checked}/{total} ({percent}%)'
    SCAN_COMPLETE = (
        '✅ <b>Сканирование завершено</b>\n\n'
        '📊 <b>Результаты:</b>\n'
        '• Проверено: {total_checked}\n'
        '• Заблокировали бота: {blocked_count}\n'
        '• Активных: {active_users}\n'
        '• Ошибок: {errors}\n'
        '• Без Telegram ID: {skipped}\n\n'
        '⏱ Время сканирования: {duration:.1f}с'
    )
    SCAN_NO_BLOCKED = '✅ <b>Отлично!</b>\n\nНе найдено пользователей, заблокировавших бота.'

    BLOCKED_LIST_TITLE = '🔒 <b>Заблокированные пользователи</b> ({count})\n\n'
    BLOCKED_USER_ROW = '• {name} (ID: <code>{telegram_id}</code>)\n'

    CLEANUP_CONFIRM_TITLE = '⚠️ <b>Подтверждение действия</b>\n\n'
    CLEANUP_CONFIRM_DELETE_DB = (
        'Вы собираетесь <b>удалить из БД</b> {count} пользователей.\n'
        'Это действие необратимо!\n\n'
        'Будут удалены:\n'
        '• Профили пользователей\n'
        '• Подписки\n'
        '• Транзакции\n'
        '• Реферальные данные'
    )
    CLEANUP_CONFIRM_DELETE_REMNAWAVE = (
        'Вы собираетесь <b>удалить из Remnawave</b> {count} пользователей.\nИх VPN доступ будет полностью отключен.'
    )
    CLEANUP_CONFIRM_DELETE_BOTH = (
        'Вы собираетесь <b>полностью удалить</b> {count} пользователей:\n'
        '• Из базы данных бота\n'
        '• Из панели Remnawave\n\n'
        'Это действие необратимо!'
    )
    CLEANUP_CONFIRM_MARK = (
        'Вы собираетесь <b>пометить как заблокированных</b> {count} пользователей.\n'
        'Они останутся в БД, но будут помечены статусом "blocked".'
    )

    CLEANUP_PROGRESS = '🗑 <b>Очистка:</b> {processed}/{total}'
    CLEANUP_COMPLETE = (
        '✅ <b>Очистка завершена</b>\n\n'
        '📊 <b>Результаты:</b>\n'
        '• Удалено из БД: {deleted_db}\n'
        '• Удалено из Remnawave: {deleted_remnawave}\n'
        '• Помечено как заблокированные: {marked}\n'
        '• Ошибок: {errors}'
    )

    BUTTON_START_SCAN = '🔍 Начать сканирование (все)'
    BUTTON_START_SCAN_SUBSCRIBERS = '🔍 Сканировать только подписчиков'
    BUTTON_VIEW_BLOCKED = '👥 Список заблокированных ({count})'
    BUTTON_PENALTY_TEST = '🧪 Тест штрафного сквада (один)'
    BUTTON_PENALTY_ALL = '🚫 Переместить всех в штрафной'
    BUTTON_DELETE_DB = '🗑 Удалить из БД'
    BUTTON_DELETE_REMNAWAVE = '🌐 Удалить из Remnawave'
    BUTTON_DELETE_BOTH = '💀 Удалить везде'
    BUTTON_MARK_BLOCKED = '🚫 Пометить как заблокированных'
    BUTTON_CONFIRM = '✅ Подтвердить'
    BUTTON_CANCEL = '❌ Отмена'
    BUTTON_BACK = '⬅️ Назад'
    BUTTON_BACK_TO_USERS = '⬅️ К пользователям'


class BlockedUsersCallback(Enum):
    """Callback data для кнопок модуля."""

    MENU = 'admin_blocked_users'
    START_SCAN = 'admin_blocked_scan'
    START_SCAN_SUBSCRIBERS = 'admin_blocked_scan_subscribers'
    PENALTY_TEST_LIST = 'admin_blocked_penalty_test_list'
    PENALTY_TEST_USER = 'admin_blocked_penalty_test_user_'
    PENALTY_TEST_CONFIRM = 'admin_blocked_penalty_test_confirm_'
    PENALTY_ALL = 'admin_blocked_penalty_all'
    PENALTY_ALL_CONFIRM = 'admin_blocked_penalty_all_confirm'
    VIEW_LIST = 'admin_blocked_list'
    VIEW_LIST_PAGE = 'admin_blocked_list_page_'
    ACTION_DELETE_DB = 'admin_blocked_action_db'
    ACTION_DELETE_REMNAWAVE = 'admin_blocked_action_rw'
    ACTION_DELETE_BOTH = 'admin_blocked_action_both'
    ACTION_MARK = 'admin_blocked_action_mark'
    CONFIRM_PREFIX = 'admin_blocked_confirm_'
    CANCEL = 'admin_blocked_cancel'


# =============================================================================
# FSM States
# =============================================================================


class BlockedUsersStates(StatesGroup):
    """Состояния FSM для модуля заблокированных пользователей."""

    scanning = State()
    viewing_results = State()
    confirming_action = State()
    processing_cleanup = State()


# =============================================================================
# Keyboards
# =============================================================================


def get_blocked_users_menu_keyboard(
    scan_result: dict[str, Any] | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура главного меню модуля."""
    buttons = [
        [
            InlineKeyboardButton(
                text=BlockedUsersText.BUTTON_START_SCAN.value,
                callback_data=BlockedUsersCallback.START_SCAN.value,
            )
        ],
        [
            InlineKeyboardButton(
                text=BlockedUsersText.BUTTON_START_SCAN_SUBSCRIBERS.value,
                callback_data=BlockedUsersCallback.START_SCAN_SUBSCRIBERS.value,
            )
        ],
    ]

    blocked_count = scan_result.get('blocked_count', 0) if scan_result else 0
    if blocked_count > 0:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=BlockedUsersText.BUTTON_VIEW_BLOCKED.value.format(count=blocked_count),
                    callback_data=BlockedUsersCallback.VIEW_LIST.value,
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=BlockedUsersText.BUTTON_PENALTY_TEST.value,
                    callback_data=BlockedUsersCallback.PENALTY_TEST_LIST.value,
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=BlockedUsersText.BUTTON_PENALTY_ALL.value,
                    callback_data=BlockedUsersCallback.PENALTY_ALL.value,
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=BlockedUsersText.BUTTON_BACK_TO_USERS.value,
                callback_data='admin_users',
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_blocked_list_keyboard(
    page: int = 1,
    total_pages: int = 1,
    has_blocked: bool = True,
) -> InlineKeyboardMarkup:
    """Клавиатура списка заблокированных пользователей."""
    buttons = []

    # Пагинация
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text='⬅️',
                    callback_data=f'{BlockedUsersCallback.VIEW_LIST_PAGE.value}{page - 1}',
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f'{page}/{total_pages}',
                callback_data='noop',
            )
        )
        if page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text='➡️',
                    callback_data=f'{BlockedUsersCallback.VIEW_LIST_PAGE.value}{page + 1}',
                )
            )
        buttons.append(nav_row)

    # Действия
    if has_blocked:
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        text=BlockedUsersText.BUTTON_DELETE_DB.value,
                        callback_data=BlockedUsersCallback.ACTION_DELETE_DB.value,
                    ),
                    InlineKeyboardButton(
                        text=BlockedUsersText.BUTTON_DELETE_REMNAWAVE.value,
                        callback_data=BlockedUsersCallback.ACTION_DELETE_REMNAWAVE.value,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=BlockedUsersText.BUTTON_DELETE_BOTH.value,
                        callback_data=BlockedUsersCallback.ACTION_DELETE_BOTH.value,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=BlockedUsersText.BUTTON_MARK_BLOCKED.value,
                        callback_data=BlockedUsersCallback.ACTION_MARK.value,
                    ),
                ],
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=BlockedUsersText.BUTTON_BACK.value,
                callback_data=BlockedUsersCallback.MENU.value,
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_confirm_keyboard(action: BlockedUserAction) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения действия."""
    action_map = {
        BlockedUserAction.DELETE_FROM_DB: 'db',
        BlockedUserAction.DELETE_FROM_REMNAWAVE: 'rw',
        BlockedUserAction.DELETE_BOTH: 'both',
        BlockedUserAction.MARK_AS_BLOCKED: 'mark',
    }

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BlockedUsersText.BUTTON_CONFIRM.value,
                    callback_data=f'{BlockedUsersCallback.CONFIRM_PREFIX.value}{action_map[action]}',
                ),
                InlineKeyboardButton(
                    text=BlockedUsersText.BUTTON_CANCEL.value,
                    callback_data=BlockedUsersCallback.CANCEL.value,
                ),
            ]
        ]
    )


# =============================================================================
# Handlers
# =============================================================================


@admin_required
@error_handler
async def show_blocked_users_menu(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Показывает главное меню модуля заблокированных пользователей."""
    data = await state.get_data()
    scan_result = data.get('blocked_users_scan_result')

    text = BlockedUsersText.MENU_TITLE.value + BlockedUsersText.MENU_DESCRIPTION.value

    if scan_result:
        text += (
            f'\n\n📊 <b>Последнее сканирование:</b>\n'
            f'• Заблокированных: {scan_result.get("blocked_count", 0)}\n'
            f'• Активных: {scan_result.get("active_users", 0)}'
        )

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_users_menu_keyboard(scan_result),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_scan(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Запускает сканирование пользователей."""
    await state.set_state(BlockedUsersStates.scanning)

    # Отправляем начальное сообщение
    await callback.message.edit_text(
        BlockedUsersText.SCAN_STARTED.value,
        parse_mode=ParseMode.HTML,
    )

    service = BlockedUsersService(bot)
    last_update_time = datetime.now(tz=UTC)

    async def progress_callback(checked: int, total: int) -> None:
        nonlocal last_update_time
        now = datetime.now(tz=UTC)
        # Обновляем сообщение не чаще раза в 3 секунды
        if (now - last_update_time).total_seconds() >= 3:
            last_update_time = now
            percent = int(checked / total * 100) if total > 0 else 0
            try:
                await callback.message.edit_text(
                    BlockedUsersText.SCAN_PROGRESS.value.format(
                        checked=checked,
                        total=total,
                        percent=percent,
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass  # Игнорируем ошибки обновления сообщения

    # Выполняем сканирование
    result = await service.scan_all_users(
        db,
        only_active=True,
        progress_callback=progress_callback,
    )

    # Сериализуем результат в dict для Redis и keyboard
    scan_result_dict = {
        'total_checked': result.total_checked,
        'blocked_count': result.blocked_count,
        'active_users': result.active_users,
        'errors': result.errors,
        'skipped_no_telegram': result.skipped_no_telegram,
        'scan_duration_seconds': result.scan_duration_seconds,
    }

    # Сохраняем результат в state
    await state.update_data(
        blocked_users_scan_result=scan_result_dict,
        blocked_users_list=[
            {
                'user_id': u.user_id,
                'telegram_id': u.telegram_id,
                'username': u.username,
                'full_name': u.full_name,
                'remnawave_uuid': u.remnawave_uuid,
                'subscription_end_date': u.subscription_end_date.isoformat() if u.subscription_end_date else None,
            }
            for u in result.blocked_users
        ],
    )

    await state.set_state(BlockedUsersStates.viewing_results)

    # Формируем итоговое сообщение
    if result.blocked_count == 0:
        text = BlockedUsersText.SCAN_NO_BLOCKED.value
    else:
        text = BlockedUsersText.SCAN_COMPLETE.value.format(
            total_checked=result.total_checked,
            blocked_count=result.blocked_count,
            active_users=result.active_users,
            errors=result.errors,
            skipped=result.skipped_no_telegram,
            duration=result.scan_duration_seconds,
        )

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_users_menu_keyboard(scan_result_dict),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_scan_subscribers(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Запускает сканирование только пользователей с активной подпиской."""
    await state.set_state(BlockedUsersStates.scanning)

    await callback.message.edit_text(
        '🔄 <b>Сканирование подписчиков...</b>\n\nПроверяются только пользователи с активной подпиской.',
        parse_mode=ParseMode.HTML,
    )

    service = BlockedUsersService(bot)
    last_update_time = datetime.now(tz=UTC)

    async def progress_callback(checked: int, total: int) -> None:
        nonlocal last_update_time
        now = datetime.now(tz=UTC)
        if (now - last_update_time).total_seconds() >= 3:
            last_update_time = now
            percent = int(checked / total * 100) if total > 0 else 0
            try:
                await callback.message.edit_text(
                    BlockedUsersText.SCAN_PROGRESS.value.format(
                        checked=checked,
                        total=total,
                        percent=percent,
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    result = await service.scan_all_users(
        db,
        only_active=True,
        only_with_active_subscription=True,
        progress_callback=progress_callback,
    )

    scan_result_dict = {
        'total_checked': result.total_checked,
        'blocked_count': result.blocked_count,
        'active_users': result.active_users,
        'errors': result.errors,
        'skipped_no_telegram': result.skipped_no_telegram,
        'scan_duration_seconds': result.scan_duration_seconds,
    }

    await state.update_data(
        blocked_users_scan_result=scan_result_dict,
        blocked_users_list=[
            {
                'user_id': u.user_id,
                'telegram_id': u.telegram_id,
                'username': u.username,
                'full_name': u.full_name,
                'remnawave_uuid': u.remnawave_uuid,
                'subscription_end_date': u.subscription_end_date.isoformat() if u.subscription_end_date else None,
            }
            for u in result.blocked_users
        ],
    )

    await state.set_state(BlockedUsersStates.viewing_results)

    if result.blocked_count == 0:
        text = '✅ <b>Отлично!</b>\n\nСреди подписчиков с активной подпиской нет заблокировавших бота.'
    else:
        text = (
            '✅ <b>Сканирование подписчиков завершено</b>\n\n'
            '📊 <b>Результаты:</b>\n'
            f'• Проверено подписчиков: {result.total_checked}\n'
            f'• Заблокировали бота: {result.blocked_count}\n'
            f'• Активных: {result.active_users}\n'
            f'• Ошибок: {result.errors}\n\n'
            f'⏱ Время: {result.scan_duration_seconds:.1f}с'
        )

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_users_menu_keyboard(scan_result_dict),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_blocked_list(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    page: int = 1,
) -> None:
    """Показывает список заблокированных пользователей."""
    data = await state.get_data()
    blocked_list: list[dict[str, Any]] = data.get('blocked_users_list', [])

    if not blocked_list:
        await callback.answer('Нет заблокированных пользователей', show_alert=True)
        return

    # Пагинация
    per_page = 15
    total_pages = (len(blocked_list) + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_users = blocked_list[start_idx:end_idx]

    text = BlockedUsersText.BLOCKED_LIST_TITLE.value.format(count=len(blocked_list))

    now = datetime.now(UTC)
    for user_data in page_users:
        name = user_data.get('full_name') or user_data.get('username') or 'Без имени'
        telegram_id = user_data.get('telegram_id', '?')

        end_date_str = user_data.get('subscription_end_date')
        if end_date_str:
            end_date = datetime.fromisoformat(end_date_str)
            days_left = (end_date.replace(tzinfo=UTC) - now).days if end_date.tzinfo else (end_date - now.replace(tzinfo=None)).days
            if days_left > 0:
                sub_info = f' — <b>{days_left} дн.</b>'
            else:
                sub_info = ' — <i>истекла</i>'
        else:
            sub_info = ' — <i>нет подписки</i>'

        text += f'• {html.escape(name)} (<code>{telegram_id}</code>){sub_info}\n'

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_list_keyboard(page, total_pages, bool(blocked_list)),
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_blocked_list_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Обрабатывает пагинацию списка заблокированных."""
    try:
        page = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        page = 1

    await show_blocked_list(callback, db_user, state, page)


@admin_required
@error_handler
async def show_action_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    action: BlockedUserAction,
) -> None:
    """Показывает подтверждение действия."""
    data = await state.get_data()
    blocked_list = data.get('blocked_users_list', [])
    count = len(blocked_list)

    if count == 0:
        await callback.answer('Нет пользователей для обработки', show_alert=True)
        return

    await state.set_state(BlockedUsersStates.confirming_action)
    await state.update_data(pending_action=action.value)

    text = BlockedUsersText.CLEANUP_CONFIRM_TITLE.value

    if action == BlockedUserAction.DELETE_FROM_DB:
        text += BlockedUsersText.CLEANUP_CONFIRM_DELETE_DB.value.format(count=count)
    elif action == BlockedUserAction.DELETE_FROM_REMNAWAVE:
        text += BlockedUsersText.CLEANUP_CONFIRM_DELETE_REMNAWAVE.value.format(count=count)
    elif action == BlockedUserAction.DELETE_BOTH:
        text += BlockedUsersText.CLEANUP_CONFIRM_DELETE_BOTH.value.format(count=count)
    elif action == BlockedUserAction.MARK_AS_BLOCKED:
        text += BlockedUsersText.CLEANUP_CONFIRM_MARK.value.format(count=count)

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_confirm_keyboard(action),
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_action_delete_db(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Обрабатывает выбор удаления из БД."""
    await show_action_confirm(callback, db_user, state, BlockedUserAction.DELETE_FROM_DB)


@admin_required
@error_handler
async def handle_action_delete_remnawave(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Обрабатывает выбор удаления из Remnawave."""
    await show_action_confirm(callback, db_user, state, BlockedUserAction.DELETE_FROM_REMNAWAVE)


@admin_required
@error_handler
async def handle_action_delete_both(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Обрабатывает выбор полного удаления."""
    await show_action_confirm(callback, db_user, state, BlockedUserAction.DELETE_BOTH)


@admin_required
@error_handler
async def handle_action_mark(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Обрабатывает выбор пометки как заблокированных."""
    await show_action_confirm(callback, db_user, state, BlockedUserAction.MARK_AS_BLOCKED)


@admin_required
@error_handler
async def handle_confirm_action(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Выполняет подтвержденное действие."""
    data = await state.get_data()
    blocked_list = data.get('blocked_users_list', [])

    # Определяем действие из callback_data
    action_code = callback.data.replace(BlockedUsersCallback.CONFIRM_PREFIX.value, '')
    action_map = {
        'db': BlockedUserAction.DELETE_FROM_DB,
        'rw': BlockedUserAction.DELETE_FROM_REMNAWAVE,
        'both': BlockedUserAction.DELETE_BOTH,
        'mark': BlockedUserAction.MARK_AS_BLOCKED,
    }
    action = action_map.get(action_code)

    if not action:
        await callback.answer('Неизвестное действие', show_alert=True)
        return

    if not blocked_list:
        await callback.answer('Нет пользователей для обработки', show_alert=True)
        return

    await state.set_state(BlockedUsersStates.processing_cleanup)

    # Преобразуем обратно в BlockCheckResult
    blocked_results = [
        BlockCheckResult(
            user_id=u['user_id'],
            telegram_id=u['telegram_id'],
            username=u['username'],
            full_name=u['full_name'],
            status=None,  # type: ignore
            remnawave_uuid=u['remnawave_uuid'],
        )
        for u in blocked_list
    ]

    service = BlockedUsersService(bot)
    last_update_time = datetime.now(tz=UTC)

    async def progress_callback(processed: int, total_count: int) -> None:
        nonlocal last_update_time
        now = datetime.now(tz=UTC)
        if (now - last_update_time).total_seconds() >= 2:
            last_update_time = now
            try:
                await callback.message.edit_text(
                    BlockedUsersText.CLEANUP_PROGRESS.value.format(
                        processed=processed,
                        total=total_count,
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    # Выполняем очистку
    result = await service.cleanup_blocked_users(
        db,
        blocked_results,
        action,
        progress_callback=progress_callback,
    )

    # Очищаем сохраненные данные
    await state.update_data(
        blocked_users_scan_result=None,
        blocked_users_list=[],
        pending_action=None,
    )
    await state.set_state(None)

    # Показываем результат
    text = BlockedUsersText.CLEANUP_COMPLETE.value.format(
        deleted_db=result.deleted_from_db,
        deleted_remnawave=result.deleted_from_remnawave,
        marked=result.marked_as_blocked,
        errors=len(result.errors),
    )

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_users_menu_keyboard(),
    )

    logger.info(
        'Очистка заблокированных пользователей завершена: DB=, RW=, marked=, errors',
        deleted_from_db=result.deleted_from_db,
        deleted_from_remnawave=result.deleted_from_remnawave,
        marked_as_blocked=result.marked_as_blocked,
        errors_count=len(result.errors),
    )

    await callback.answer()


@admin_required
@error_handler
async def handle_cancel(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Отменяет текущее действие и возвращает в меню."""
    await state.update_data(pending_action=None)
    await state.set_state(BlockedUsersStates.viewing_results)
    await show_blocked_users_menu(callback, db_user, state)


# =============================================================================
# Penalty squad handlers
# =============================================================================


@admin_required
@error_handler
async def show_penalty_test_list(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Показывает список для тестового перемещения одного пользователя."""
    if not settings.PENALTY_SQUAD_ENABLED:
        await callback.answer('Штрафной сквад отключён в настройках конфига.', show_alert=True)
        return
    if not settings.PENALTY_SQUAD_UUID:
        await callback.answer('Не задан PENALTY_SQUAD_UUID в настройках.', show_alert=True)
        return

    data = await state.get_data()
    blocked_list: list[dict[str, Any]] = data.get('blocked_users_list', [])

    if not blocked_list:
        await callback.answer('Список пуст — сначала запустите сканирование.', show_alert=True)
        return

    # Показываем первых 10 пользователей кнопками (без уже помеченных)
    buttons = []
    shown = 0
    for u in blocked_list[:20]:
        if shown >= 10:
            break
        name = u.get('full_name') or u.get('username') or 'Без имени'
        tid = u.get('telegram_id', '?')
        user_id = u.get('user_id')
        end_date_str = u.get('subscription_end_date')
        sub_label = ''
        if end_date_str:
            from datetime import UTC, datetime
            end_date = datetime.fromisoformat(end_date_str)
            days_left = (end_date.replace(tzinfo=UTC) - datetime.now(UTC)).days
            sub_label = f' [{days_left}д]' if days_left > 0 else ' [истекла]'

        buttons.append([
            InlineKeyboardButton(
                text=f'{html.unescape(name)}{sub_label} ({tid})',
                callback_data=f'{BlockedUsersCallback.PENALTY_TEST_USER.value}{user_id}',
            )
        ])
        shown += 1

    buttons.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=BlockedUsersCallback.MENU.value)])

    await callback.message.edit_text(
        f'🧪 <b>Тест штрафного сквада</b>\n\nВыбери одного пользователя для тестового перемещения:\n'
        f'<i>Показано {shown} из {len(blocked_list)}</i>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@admin_required
@error_handler
async def penalty_test_user_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Запрашивает подтверждение перемещения конкретного пользователя."""
    user_id = int(callback.data.split('_')[-1])
    data = await state.get_data()
    blocked_list: list[dict[str, Any]] = data.get('blocked_users_list', [])

    user_data = next((u for u in blocked_list if u.get('user_id') == user_id), None)
    if not user_data:
        await callback.answer('Пользователь не найден в списке.', show_alert=True)
        return

    name = user_data.get('full_name') or user_data.get('username') or 'Без имени'
    tid = user_data.get('telegram_id', '?')

    await callback.message.edit_text(
        f'🧪 <b>Подтверждение теста</b>\n\n'
        f'Пользователь: <b>{html.escape(name)}</b>\n'
        f'Telegram ID: <code>{tid}</code>\n\n'
        f'Штрафной сквад: <code>{settings.PENALTY_SQUAD_UUID}</code>\n\n'
        f'Переместить этого пользователя в штрафной сквад?',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='✅ Переместить',
                    callback_data=f'{BlockedUsersCallback.PENALTY_TEST_CONFIRM.value}{user_id}',
                ),
                InlineKeyboardButton(text='❌ Отмена', callback_data=BlockedUsersCallback.PENALTY_TEST_LIST.value),
            ]
        ]),
    )
    await callback.answer()


@admin_required
@error_handler
async def penalty_test_user_execute(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Выполняет перемещение одного пользователя в штрафной сквад."""
    user_id = int(callback.data.split('_')[-1])

    target_user = await get_user_by_id(db, user_id)
    if not target_user:
        await callback.answer('Пользователь не найден в БД.', show_alert=True)
        return

    await callback.message.edit_text('⏳ Перемещаю...', parse_mode=ParseMode.HTML)
    await callback.answer()

    ok = await penalize_user(db, target_user)

    if ok:
        await callback.message.edit_text(
            f'✅ <b>Готово!</b>\n\n'
            f'Пользователь <code>{target_user.telegram_id}</code> перемещён в штрафной сквад.\n'
            f'Сквад: <code>{settings.PENALTY_SQUAD_UUID}</code>\n\n'
            f'<i>is_penalized = True</i>',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='⬅️ К списку', callback_data=BlockedUsersCallback.PENALTY_TEST_LIST.value)],
                [InlineKeyboardButton(text='🏠 Меню', callback_data=BlockedUsersCallback.MENU.value)],
            ]),
        )
    else:
        await callback.message.edit_text(
            f'❌ <b>Ошибка</b>\n\nНе удалось переместить пользователя. '
            f'Проверь логи и настройку PENALTY_SQUAD_UUID.',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='⬅️ Назад', callback_data=BlockedUsersCallback.MENU.value)],
            ]),
        )


@admin_required
@error_handler
async def penalty_all_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Подтверждение массового перемещения в штрафной сквад."""
    if not settings.PENALTY_SQUAD_ENABLED or not settings.PENALTY_SQUAD_UUID:
        await callback.answer('Штрафной сквад не настроен.', show_alert=True)
        return

    data = await state.get_data()
    blocked_list: list[dict[str, Any]] = data.get('blocked_users_list', [])
    count = len(blocked_list)

    await callback.message.edit_text(
        f'⚠️ <b>Массовое перемещение в штрафной сквад</b>\n\n'
        f'Будет перемещено: <b>{count}</b> пользователей\n'
        f'Сквад: <code>{settings.PENALTY_SQUAD_UUID}</code>\n\n'
        f'Подтвердить?',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text='✅ Переместить всех', callback_data=BlockedUsersCallback.PENALTY_ALL_CONFIRM.value),
                InlineKeyboardButton(text='❌ Отмена', callback_data=BlockedUsersCallback.MENU.value),
            ]
        ]),
    )
    await callback.answer()


@admin_required
@error_handler
async def penalty_all_execute(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Массовое перемещение в штрафной сквад."""
    data = await state.get_data()
    blocked_list: list[dict[str, Any]] = data.get('blocked_users_list', [])

    if not blocked_list:
        await callback.answer('Список пуст.', show_alert=True)
        return

    total = len(blocked_list)
    success = 0
    failed = 0

    await callback.message.edit_text(f'⏳ Перемещаю 0/{total}...', parse_mode=ParseMode.HTML)
    await callback.answer()

    last_edit = datetime.now(UTC)

    for i, u in enumerate(blocked_list, 1):
        user_id = u.get('user_id')
        if not user_id:
            failed += 1
            continue

        target_user = await get_user_by_id(db, user_id)
        if not target_user:
            failed += 1
            continue

        ok = await penalize_user(db, target_user)
        if ok:
            success += 1
        else:
            failed += 1

        now = datetime.now(UTC)
        if (now - last_edit).total_seconds() >= 3:
            last_edit = now
            try:
                await callback.message.edit_text(
                    f'⏳ Перемещаю {i}/{total}... ✅{success} ❌{failed}',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    await callback.message.edit_text(
        f'✅ <b>Готово!</b>\n\n'
        f'• Перемещено: {success}\n'
        f'• Ошибок: {failed}\n\n'
        f'Сквад: <code>{settings.PENALTY_SQUAD_UUID}</code>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🏠 Меню', callback_data=BlockedUsersCallback.MENU.value)],
        ]),
    )


# =============================================================================
# Registration
# =============================================================================


def register_handlers(dp: Dispatcher) -> None:
    """Регистрирует хендлеры модуля заблокированных пользователей."""

    # Главное меню
    dp.callback_query.register(
        show_blocked_users_menu,
        F.data == BlockedUsersCallback.MENU.value,
    )

    # Сканирование
    dp.callback_query.register(
        start_scan,
        F.data == BlockedUsersCallback.START_SCAN.value,
    )
    dp.callback_query.register(
        start_scan_subscribers,
        F.data == BlockedUsersCallback.START_SCAN_SUBSCRIBERS.value,
    )

    # Список заблокированных
    dp.callback_query.register(
        show_blocked_list,
        F.data == BlockedUsersCallback.VIEW_LIST.value,
    )

    # Пагинация списка
    dp.callback_query.register(
        handle_blocked_list_pagination,
        F.data.startswith(BlockedUsersCallback.VIEW_LIST_PAGE.value),
    )

    # Выбор действий
    dp.callback_query.register(
        handle_action_delete_db,
        F.data == BlockedUsersCallback.ACTION_DELETE_DB.value,
    )
    dp.callback_query.register(
        handle_action_delete_remnawave,
        F.data == BlockedUsersCallback.ACTION_DELETE_REMNAWAVE.value,
    )
    dp.callback_query.register(
        handle_action_delete_both,
        F.data == BlockedUsersCallback.ACTION_DELETE_BOTH.value,
    )
    dp.callback_query.register(
        handle_action_mark,
        F.data == BlockedUsersCallback.ACTION_MARK.value,
    )

    # Подтверждение действий
    dp.callback_query.register(
        handle_confirm_action,
        F.data.startswith(BlockedUsersCallback.CONFIRM_PREFIX.value),
    )

    # Отмена
    dp.callback_query.register(
        handle_cancel,
        F.data == BlockedUsersCallback.CANCEL.value,
    )

    # Штрафной сквад
    dp.callback_query.register(
        show_penalty_test_list,
        F.data == BlockedUsersCallback.PENALTY_TEST_LIST.value,
    )
    dp.callback_query.register(
        penalty_test_user_confirm,
        F.data.startswith(BlockedUsersCallback.PENALTY_TEST_USER.value),
    )
    dp.callback_query.register(
        penalty_test_user_execute,
        F.data.startswith(BlockedUsersCallback.PENALTY_TEST_CONFIRM.value),
    )
    dp.callback_query.register(
        penalty_all_confirm,
        F.data == BlockedUsersCallback.PENALTY_ALL.value,
    )
    dp.callback_query.register(
        penalty_all_execute,
        F.data == BlockedUsersCallback.PENALTY_ALL_CONFIRM.value,
    )
