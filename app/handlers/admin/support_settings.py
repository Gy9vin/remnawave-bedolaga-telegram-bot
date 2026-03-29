import contextlib
import html
import re
from pathlib import Path

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.support_settings_service import SupportSettingsService
from app.states import SupportSettingsStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _get_support_settings_keyboard(language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    mode = SupportSettingsService.get_system_mode()
    menu_enabled = SupportSettingsService.is_support_menu_enabled()
    admin_notif = SupportSettingsService.get_admin_ticket_notifications_enabled()
    user_notif = SupportSettingsService.get_user_ticket_notifications_enabled()
    sla_enabled = SupportSettingsService.get_sla_enabled()
    sla_minutes = SupportSettingsService.get_sla_minutes()

    rows: list[list[types.InlineKeyboardButton]] = []

    status_enabled = texts.t('ADMIN_SUPPORT_SETTINGS_STATUS_ENABLED', 'Включены')
    status_disabled = texts.t('ADMIN_SUPPORT_SETTINGS_STATUS_DISABLED', 'Отключены')

    def mode_button(label_key: str, default: str, active: bool) -> str:
        prefix = '🔘' if active else '⚪'
        return f'{prefix} {texts.t(label_key, default)}'

    rows.append(
        [
            types.InlineKeyboardButton(
                text=(
                    f'{"✅" if menu_enabled else "🚫"} '
                    f'{texts.t("ADMIN_SUPPORT_SETTINGS_MENU_LABEL", "Пункт «Техподдержка» в меню")}'
                ),
                callback_data='admin_support_toggle_menu',
            )
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=mode_button('ADMIN_SUPPORT_SETTINGS_MODE_TICKETS', 'Тикеты', mode == 'tickets'),
                callback_data='admin_support_mode_tickets',
            ),
            types.InlineKeyboardButton(
                text=mode_button('ADMIN_SUPPORT_SETTINGS_MODE_CONTACT', 'Контакт', mode == 'contact'),
                callback_data='admin_support_mode_contact',
            ),
            types.InlineKeyboardButton(
                text=mode_button('ADMIN_SUPPORT_SETTINGS_MODE_BOTH', 'Оба', mode == 'both'),
                callback_data='admin_support_mode_both',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_EDIT_DESCRIPTION', '📝 Изменить описание'),
                callback_data='admin_support_edit_desc',
            )
        ]
    )

    # Notifications block
    rows.append(
        [
            types.InlineKeyboardButton(
                text=(
                    f'{"🔔" if admin_notif else "🔕"} '
                    f'{texts.t("ADMIN_SUPPORT_SETTINGS_ADMIN_NOTIFICATIONS", "Админ-уведомления")}: '
                    f'{status_enabled if admin_notif else status_disabled}'
                ),
                callback_data='admin_support_toggle_admin_notifications',
            )
        ]
    )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=(
                    f'{"🔔" if user_notif else "🔕"} '
                    f'{texts.t("ADMIN_SUPPORT_SETTINGS_USER_NOTIFICATIONS", "Пользовательские уведомления")}: '
                    f'{status_enabled if user_notif else status_disabled}'
                ),
                callback_data='admin_support_toggle_user_notifications',
            )
        ]
    )

    # SLA block
    rows.append(
        [
            types.InlineKeyboardButton(
                text=(
                    f'{"⏰" if sla_enabled else "⏹️"} '
                    f'{texts.t("ADMIN_SUPPORT_SETTINGS_SLA_LABEL", "SLA")}: '
                    f'{status_enabled if sla_enabled else status_disabled}'
                ),
                callback_data='admin_support_toggle_sla',
            )
        ]
    )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_SLA_TIME', '⏳ Время SLA: {minutes} мин').format(
                    minutes=sla_minutes
                ),
                callback_data='admin_support_set_sla_minutes',
            )
        ]
    )

    # Moderators
    moderators = SupportSettingsService.get_moderators()
    mod_count = len(moderators)
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_MODERATORS_COUNT', '🧑‍⚖️ Модераторы: {count}').format(
                    count=mod_count
                ),
                callback_data='admin_support_list_moderators',
            )
        ]
    )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_ADD_MODERATOR', '➕ Назначить модератора'),
                callback_data='admin_support_add_moderator',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_REMOVE_MODERATOR', '➖ Удалить модератора'),
                callback_data='admin_support_remove_moderator',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text='🤖 Настройки ИИ',
                callback_data='admin_support_ai_settings',
            )
        ]
    )

    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_support')])

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _get_ai_settings_keyboard(language: str) -> types.InlineKeyboardMarkup:
    ai_mode = SupportSettingsService.get_ticket_ai_mode()
    ai_style = SupportSettingsService.get_ai_style()

    style_labels = {
        'friendly': '😊 Дружелюбный',
        'formal': '🎩 Официальный',
        'brief': '⚡ Краткий',
        'empathetic': '💙 Участливый',
    }

    rows: list[list[types.InlineKeyboardButton]] = []

    # AI режим тикетов
    rows.append(
        [
            types.InlineKeyboardButton(
                text=f'{"🔘" if ai_mode == "off" else "⚪"} ❌ Выключено',
                callback_data='admin_support_ai_mode_off',
            ),
            types.InlineKeyboardButton(
                text=f'{"🔘" if ai_mode == "normal" else "⚪"} 💬 Обычный',
                callback_data='admin_support_ai_mode_normal',
            ),
            types.InlineKeyboardButton(
                text=f'{"🔘" if ai_mode == "ai" else "⚪"} 🤖 С ИИ',
                callback_data='admin_support_ai_mode_ai',
            ),
        ]
    )

    # Имена ИИ
    ai_names = SupportSettingsService.get_ai_names()
    names_preview = ', '.join(ai_names[:3]) + ('...' if len(ai_names) > 3 else '')
    test_tid = SupportSettingsService.get_ai_test_telegram_id()
    rows.append(
        [
            types.InlineKeyboardButton(
                text=f'📛 Имена ИИ: {names_preview}',
                callback_data='admin_support_ai_set_name',
            )
        ]
    )

    # Тест-режим
    rows.append(
        [
            types.InlineKeyboardButton(
                text=f'🧪 Тест: {"ID " + str(test_tid) if test_tid else "Выключен"}',
                callback_data='admin_support_ai_test_mode',
            )
        ]
    )

    # Стиль ответов
    rows.append(
        [
            types.InlineKeyboardButton(
                text=f'🎭 Стиль: {style_labels.get(ai_style, ai_style)}',
                callback_data='admin_support_ai_style_menu',
            )
        ]
    )

    # База знаний и правила
    rows.append(
        [
            types.InlineKeyboardButton(
                text='📚 База знаний',
                callback_data='admin_support_ai_view_kb',
            ),
            types.InlineKeyboardButton(
                text='📋 Правила ИИ',
                callback_data='admin_support_ai_view_rules',
            ),
        ]
    )

    rows.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='admin_support_settings')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _get_ai_style_keyboard() -> types.InlineKeyboardMarkup:
    current = SupportSettingsService.get_ai_style()
    styles = [
        ('friendly', '😊 Дружелюбный'),
        ('formal', '🎩 Официальный'),
        ('brief', '⚡ Краткий'),
        ('empathetic', '💙 Участливый'),
    ]
    rows = [
        [
            types.InlineKeyboardButton(
                text=f'{"✅ " if current == key else ""}{label}',
                callback_data=f'admin_support_ai_style_{key}',
            )
        ]
        for key, label in styles
    ]
    rows.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='admin_support_ai_settings')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@admin_required
@error_handler
async def show_support_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    desc = SupportSettingsService.get_support_info_text(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_SUPPORT_SETTINGS_TITLE', '🛟 <b>Настройки поддержки</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SUPPORT_SETTINGS_DESCRIPTION',
            'Режим работы и видимость в меню. Ниже текущее описание меню поддержки:',
        )
        + '\n\n'
        + desc,
        reply_markup=_get_support_settings_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_support_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current = SupportSettingsService.is_support_menu_enabled()
    SupportSettingsService.set_support_menu_enabled(not current)
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def toggle_admin_notifications(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current = SupportSettingsService.get_admin_ticket_notifications_enabled()
    SupportSettingsService.set_admin_ticket_notifications_enabled(not current)
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def toggle_user_notifications(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current = SupportSettingsService.get_user_ticket_notifications_enabled()
    SupportSettingsService.set_user_ticket_notifications_enabled(not current)
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def toggle_sla(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current = SupportSettingsService.get_sla_enabled()
    SupportSettingsService.set_sla_enabled(not current)
    await show_support_settings(callback, db_user, db)


class SupportAdvancedStates(StatesGroup):
    waiting_for_sla_minutes = State()
    waiting_for_moderator_id = State()


@admin_required
@error_handler
async def start_set_sla_minutes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SUPPORT_SLA_SETUP_PROMPT',
            '⏳ <b>Настройка SLA</b>\n\nВведите количество минут ожидания ответа (целое число > 0):',
        ),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')]]
        ),
    )
    await state.set_state(SupportAdvancedStates.waiting_for_sla_minutes)
    await callback.answer()


@admin_required
@error_handler
async def handle_sla_minutes(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    text = (message.text or '').strip()
    try:
        minutes = int(text)
        if minutes <= 0 or minutes > 1440:
            raise ValueError
    except Exception:
        await message.answer(texts.t('ADMIN_SUPPORT_SLA_INVALID', '❌ Введите корректное число минут (1-1440)'))
        return
    SupportSettingsService.set_sla_minutes(minutes)
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='admin_support_delete_msg'
                )
            ]
        ]
    )
    await message.answer(texts.t('ADMIN_SUPPORT_SLA_SAVED', '✅ Значение SLA сохранено'), reply_markup=markup)


@admin_required
@error_handler
async def start_add_moderator(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SUPPORT_ASSIGN_MODERATOR_PROMPT',
            '🧑‍⚖️ <b>Назначение модератора</b>\n\nОтправьте Telegram ID пользователя (число)',
        ),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')]]
        ),
    )
    await state.set_state(SupportAdvancedStates.waiting_for_moderator_id)
    await callback.answer()


@admin_required
@error_handler
async def start_remove_moderator(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SUPPORT_REMOVE_MODERATOR_PROMPT',
            '🧑‍⚖️ <b>Удаление модератора</b>\n\nОтправьте Telegram ID пользователя (число)',
        ),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')]]
        ),
    )
    await state.set_state(SupportAdvancedStates.waiting_for_moderator_id)
    # We'll reuse the same state; next message will decide action via flag
    await state.update_data(action='remove_moderator')
    await callback.answer()


@admin_required
@error_handler
async def handle_moderator_id(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    action = data.get('action', 'add')
    text = (message.text or '').strip()
    try:
        tid = int(text)
    except Exception:
        await message.answer(texts.t('ADMIN_SUPPORT_INVALID_TELEGRAM_ID', '❌ Введите корректный Telegram ID (число)'))
        return
    if action == 'remove_moderator':
        ok = SupportSettingsService.remove_moderator(tid)
        msg = (
            texts.t('ADMIN_SUPPORT_MODERATOR_REMOVED_SUCCESS', '✅ Модератор {tid} удалён').format(tid=tid)
            if ok
            else texts.t('ADMIN_SUPPORT_MODERATOR_REMOVED_FAIL', '❌ Не удалось удалить модератора')
        )
    else:
        ok = SupportSettingsService.add_moderator(tid)
        msg = (
            texts.t('ADMIN_SUPPORT_MODERATOR_ADDED_SUCCESS', '✅ Пользователь {tid} назначен модератором').format(
                tid=tid
            )
            if ok
            else texts.t('ADMIN_SUPPORT_MODERATOR_ADDED_FAIL', '❌ Не удалось назначить модератора')
        )
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='admin_support_delete_msg'
                )
            ]
        ]
    )
    await message.answer(msg, reply_markup=markup)


@admin_required
@error_handler
async def list_moderators(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    moderators = SupportSettingsService.get_moderators()
    if not moderators:
        await callback.answer(texts.t('ADMIN_SUPPORT_MODERATORS_EMPTY', 'Список пуст'), show_alert=True)
        return
    text = (
        texts.t('ADMIN_SUPPORT_MODERATORS_TITLE', '🧑‍⚖️ <b>Модераторы</b>')
        + '\n\n'
        + '\n'.join([f'• <code>{tid}</code>' for tid in moderators])
    )
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')]]
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=markup)
    await callback.answer()


@admin_required
@error_handler
async def set_mode_tickets(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_system_mode('tickets')
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def set_mode_contact(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_system_mode('contact')
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def set_mode_both(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_system_mode('both')
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def start_edit_desc(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    current_desc_html = SupportSettingsService.get_support_info_text(db_user.language)
    # plain text for display-only code block
    current_desc_plain = re.sub(r'<[^>]+>', '', current_desc_html)

    kb_rows: list[list[types.InlineKeyboardButton]] = []
    kb_rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SEND_DESCRIPTION', '📨 Прислать текст'),
                callback_data='admin_support_send_desc',
            )
        ]
    )
    # Подготовим блок контакта (отдельным инлайном)
    from app.config import settings

    support_contact_display = settings.get_support_contact_display()
    kb_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')])

    text_parts = [
        texts.t('ADMIN_SUPPORT_EDIT_DESCRIPTION_TITLE', '📝 <b>Редактирование описания поддержки</b>'),
        '',
        texts.t('ADMIN_SUPPORT_EDIT_DESCRIPTION_CURRENT', 'Текущее описание:'),
        '',
        f'<code>{html.escape(current_desc_plain)}</code>',
    ]
    if support_contact_display:
        text_parts += [
            '',
            texts.t('ADMIN_SUPPORT_EDIT_DESCRIPTION_CONTACT_TITLE', '<b>Контакт для режима «Контакт»</b>'),
            f'<code>{html.escape(support_contact_display)}</code>',
            '',
            texts.t('ADMIN_SUPPORT_EDIT_DESCRIPTION_CONTACT_HINT', 'Добавьте в описание при необходимости.'),
        ]
    await callback.message.edit_text(
        '\n'.join(text_parts), reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode='HTML'
    )
    await state.set_state(SupportSettingsStates.waiting_for_desc)
    await callback.answer()


@admin_required
@error_handler
async def handle_new_desc(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    new_text = message.html_text or message.text
    SupportSettingsService.set_support_info_text(db_user.language, new_text)
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='admin_support_delete_msg'
                )
            ]
        ]
    )
    await message.answer(texts.t('ADMIN_SUPPORT_DESCRIPTION_UPDATED', '✅ Описание обновлено.'), reply_markup=markup)


@admin_required
@error_handler
async def send_desc_copy(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # send plain text for easy copying
    texts = get_texts(db_user.language)
    current_desc_html = SupportSettingsService.get_support_info_text(db_user.language)
    current_desc_plain = re.sub(r'<[^>]+>', '', current_desc_html)
    # attach delete button to the sent message
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='admin_support_delete_msg'
                )
            ]
        ]
    )
    if len(current_desc_plain) <= 4000:
        await callback.message.answer(current_desc_plain, reply_markup=markup)
    else:
        # split long messages (attach delete only to the last chunk)
        chunk = 0
        while chunk < len(current_desc_plain):
            next_chunk = current_desc_plain[chunk : chunk + 4000]
            is_last = (chunk + 4000) >= len(current_desc_plain)
            await callback.message.answer(next_chunk, reply_markup=(markup if is_last else None))
            chunk += 4000
    await callback.answer(texts.t('ADMIN_SUPPORT_DESCRIPTION_SENT', 'Текст отправлен ниже'))


@error_handler
async def delete_sent_message(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Allow admins and moderators to delete informational notifications
    try:
        may_delete = settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(
            callback.from_user.id
        )
    except Exception:
        may_delete = False
    texts = get_texts(db_user.language if db_user else 'ru')
    if not may_delete:
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    try:
        await callback.message.delete()
    finally:
        with contextlib.suppress(Exception):
            await callback.answer(texts.t('ADMIN_SUPPORT_MESSAGE_DELETED', 'Сообщение удалено'))


@admin_required
@error_handler
async def show_ai_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    ai_mode = SupportSettingsService.get_ticket_ai_mode()
    mode_labels = {'off': 'Выключен', 'normal': 'Обычный', 'ai': 'С ИИ'}
    ai_names = SupportSettingsService.get_ai_names()
    test_tid = SupportSettingsService.get_ai_test_telegram_id()
    gigachat_key = getattr(settings, 'GIGACHAT_AUTH_KEY', None)
    gigachat_status = '✅ Настроен' if gigachat_key else '❌ Не настроен (GIGACHAT_AUTH_KEY)'
    test_status = f'🧪 Тест-режим: только ID <code>{test_tid}</code>' if test_tid else ''
    text = (
        f'🤖 <b>Настройки ИИ в тикетах</b>\n\n'
        f'Режим: <b>{mode_labels.get(ai_mode, ai_mode)}</b>\n'
        f'Имена ИИ: <b>{", ".join(ai_names)}</b>\n'
        f'GigaChat: {gigachat_status}\n'
        + (f'{test_status}\n' if test_status else '')
        + '\n<i>В режиме «С ИИ» бот автоматически отвечает пользователям. '
        'Оператор может вмешаться в любой момент.</i>'
    )
    await callback.message.edit_text(
        text,
        reply_markup=_get_ai_settings_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def set_ai_mode_off(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_ticket_ai_mode('off')
    await show_ai_settings(callback, db_user, db)


@admin_required
@error_handler
async def set_ai_mode_normal(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_ticket_ai_mode('normal')
    await show_ai_settings(callback, db_user, db)


@admin_required
@error_handler
async def set_ai_mode_ai(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_ticket_ai_mode('ai')
    await show_ai_settings(callback, db_user, db)


@admin_required
@error_handler
async def start_set_ai_name(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    current_names = SupportSettingsService.get_ai_names()
    await callback.message.edit_text(
        f'📛 <b>Имена ИИ-агента</b>\n\n'
        f'Текущие имена: <b>{", ".join(current_names)}</b>\n\n'
        f'Отправьте имена через запятую (например: <code>Алиса, Маша, Катя</code>)\n'
        f'При каждом новом тикете случайно выбирается одно имя:',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='◀️ Назад', callback_data='admin_support_ai_settings')]]
        ),
    )
    await state.set_state(SupportSettingsStates.waiting_for_ai_name)
    await callback.answer()


@admin_required
@error_handler
async def handle_ai_name(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').strip()
    names = [n.strip() for n in raw.split(',') if n.strip()]
    invalid = [n for n in names if len(n) > 32]
    if not names:
        await message.answer('❌ Введите хотя бы одно имя')
        return
    if invalid:
        await message.answer(f'❌ Слишком длинные имена (макс. 32 символа): {", ".join(invalid)}')
        return
    ok = SupportSettingsService.set_ai_names(names)
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]]
    )
    names_str = html.escape(', '.join(names))
    msg = f'✅ Имена ИИ сохранены: <b>{names_str}</b>' if ok else '❌ Не удалось сохранить'
    await message.answer(msg, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def show_ai_test_mode(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    test_tid = SupportSettingsService.get_ai_test_telegram_id()
    status = f'Активен — только для ID <code>{test_tid}</code>' if test_tid else 'Выключен (AI отвечает всем)'
    rows: list[list[types.InlineKeyboardButton]] = []
    if test_tid:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='🔴 Выключить тест-режим',
                    callback_data='admin_support_ai_test_clear',
                )
            ]
        )
    rows.append(
        [
            types.InlineKeyboardButton(
                text='🆔 Задать Telegram ID для теста',
                callback_data='admin_support_ai_test_set',
            )
        ]
    )
    rows.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='admin_support_ai_settings')])
    await callback.message.edit_text(
        f'🧪 <b>Тест-режим ИИ</b>\n\n'
        f'Статус: {status}\n\n'
        f'<i>В тест-режиме AI отвечает только указанному аккаунту — удобно для обучения без участия реальных пользователей.</i>',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_set_ai_test_id(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await callback.message.edit_text(
        '🧪 <b>Тест-режим — ввод Telegram ID</b>\n\n'
        'Отправьте Telegram ID аккаунта для тестирования.\n'
        '<i>AI будет отвечать ТОЛЬКО этому пользователю.</i>',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='◀️ Назад', callback_data='admin_support_ai_test_mode')]]
        ),
    )
    await state.set_state(SupportSettingsStates.waiting_for_ai_test_id)
    await callback.answer()


@admin_required
@error_handler
async def handle_ai_test_id(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').strip()
    try:
        tid = int(raw)
    except ValueError:
        await message.answer('❌ Введите числовой Telegram ID')
        return
    ok = SupportSettingsService.set_ai_test_telegram_id(tid)
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]]
    )
    await message.answer(
        f'✅ Тест-режим активирован для ID <code>{tid}</code>' if ok else '❌ Не удалось сохранить',
        reply_markup=markup,
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def clear_ai_test_mode(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.clear_ai_test_telegram_id()
    await show_ai_settings(callback, db_user, db)


@admin_required
@error_handler
async def show_ai_style_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '🎭 <b>Стиль ответов ИИ</b>\n\nВыберите стиль общения:',
        parse_mode='HTML',
        reply_markup=_get_ai_style_keyboard(),
    )
    await callback.answer()


@admin_required
@error_handler
async def set_ai_style_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    style = (callback.data or '').replace('admin_support_ai_style_', '')
    SupportSettingsService.set_ai_style(style)
    await show_ai_settings(callback, db_user, db)


@admin_required
@error_handler
async def view_knowledge_base(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    kb_path = Path(__file__).parent.parent.parent / 'services' / 'ai_support' / 'knowledge_base.md'
    try:
        content = kb_path.read_text(encoding='utf-8')
    except Exception:
        content = '(файл не найден)'

    preview = content[:3000] + ('...' if len(content) > 3000 else '')
    await callback.message.edit_text(
        f'📚 <b>База знаний ИИ</b>\n\n<code>{html.escape(preview)}</code>\n\n'
        f'Отправьте новый текст чтобы заменить содержимое:',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='📨 Получить полный текст', callback_data='admin_support_ai_send_kb')],
                [types.InlineKeyboardButton(text='◀️ Назад', callback_data='admin_support_ai_settings')],
            ]
        ),
    )
    await state.set_state(SupportSettingsStates.waiting_for_kb_text)
    await callback.answer()


@admin_required
@error_handler
async def send_kb_copy(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    kb_path = Path(__file__).parent.parent.parent / 'services' / 'ai_support' / 'knowledge_base.md'
    try:
        content = kb_path.read_text(encoding='utf-8')
    except Exception:
        content = '(файл не найден)'
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]]
    )
    await callback.message.answer(content, reply_markup=markup)
    await callback.answer('Текст отправлен ниже')


@admin_required
@error_handler
async def handle_kb_text(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    kb_path = Path(__file__).parent.parent.parent / 'services' / 'ai_support' / 'knowledge_base.md'
    new_text = (message.text or '').strip()
    if not new_text:
        await message.answer('❌ Текст не может быть пустым')
        return
    try:
        kb_path.write_text(new_text, encoding='utf-8')
        ok = True
    except Exception:
        ok = False
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]]
    )
    await message.answer(
        '✅ База знаний обновлена' if ok else '❌ Не удалось сохранить файл',
        reply_markup=markup,
    )


@admin_required
@error_handler
async def view_ai_rules(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    rules_path = Path(__file__).parent.parent.parent / 'services' / 'ai_support' / 'ai_rules.md'
    try:
        content = rules_path.read_text(encoding='utf-8')
    except Exception:
        content = '(файл не найден)'

    preview = content[:3000] + ('...' if len(content) > 3000 else '')
    await callback.message.edit_text(
        f'📋 <b>Правила поведения ИИ</b>\n\n<code>{html.escape(preview)}</code>\n\n'
        f'Отправьте новый текст чтобы заменить правила:',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='📨 Получить полный текст', callback_data='admin_support_ai_send_rules'
                    )
                ],
                [types.InlineKeyboardButton(text='◀️ Назад', callback_data='admin_support_ai_settings')],
            ]
        ),
    )
    await state.set_state(SupportSettingsStates.waiting_for_rules_text)
    await callback.answer()


@admin_required
@error_handler
async def send_rules_copy(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    rules_path = Path(__file__).parent.parent.parent / 'services' / 'ai_support' / 'ai_rules.md'
    try:
        content = rules_path.read_text(encoding='utf-8')
    except Exception:
        content = '(файл не найден)'
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]]
    )
    await callback.message.answer(content, reply_markup=markup)
    await callback.answer('Текст отправлен ниже')


@admin_required
@error_handler
async def handle_rules_text(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    rules_path = Path(__file__).parent.parent.parent / 'services' / 'ai_support' / 'ai_rules.md'
    new_text = (message.text or '').strip()
    if not new_text:
        await message.answer('❌ Текст не может быть пустым')
        return
    try:
        rules_path.write_text(new_text, encoding='utf-8')
        ok = True
    except Exception:
        ok = False
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]]
    )
    await message.answer(
        '✅ Правила ИИ обновлены' if ok else '❌ Не удалось сохранить файл',
        reply_markup=markup,
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_support_settings, F.data == 'admin_support_settings')
    dp.callback_query.register(toggle_support_menu, F.data == 'admin_support_toggle_menu')
    dp.callback_query.register(set_mode_tickets, F.data == 'admin_support_mode_tickets')
    dp.callback_query.register(set_mode_contact, F.data == 'admin_support_mode_contact')
    dp.callback_query.register(set_mode_both, F.data == 'admin_support_mode_both')
    dp.callback_query.register(start_edit_desc, F.data == 'admin_support_edit_desc')
    dp.callback_query.register(send_desc_copy, F.data == 'admin_support_send_desc')
    dp.callback_query.register(delete_sent_message, F.data == 'admin_support_delete_msg')
    dp.callback_query.register(toggle_admin_notifications, F.data == 'admin_support_toggle_admin_notifications')
    dp.callback_query.register(toggle_user_notifications, F.data == 'admin_support_toggle_user_notifications')
    dp.callback_query.register(toggle_sla, F.data == 'admin_support_toggle_sla')
    dp.callback_query.register(start_set_sla_minutes, F.data == 'admin_support_set_sla_minutes')
    dp.callback_query.register(start_add_moderator, F.data == 'admin_support_add_moderator')
    dp.callback_query.register(start_remove_moderator, F.data == 'admin_support_remove_moderator')
    dp.callback_query.register(list_moderators, F.data == 'admin_support_list_moderators')
    dp.callback_query.register(show_ai_settings, F.data == 'admin_support_ai_settings')
    dp.callback_query.register(set_ai_mode_off, F.data == 'admin_support_ai_mode_off')
    dp.callback_query.register(set_ai_mode_normal, F.data == 'admin_support_ai_mode_normal')
    dp.callback_query.register(set_ai_mode_ai, F.data == 'admin_support_ai_mode_ai')
    dp.callback_query.register(start_set_ai_name, F.data == 'admin_support_ai_set_name')
    dp.callback_query.register(show_ai_style_menu, F.data == 'admin_support_ai_style_menu')
    dp.callback_query.register(
        set_ai_style_handler,
        F.data.in_(
            {
                'admin_support_ai_style_friendly',
                'admin_support_ai_style_formal',
                'admin_support_ai_style_brief',
                'admin_support_ai_style_empathetic',
            }
        ),
    )
    dp.callback_query.register(view_knowledge_base, F.data == 'admin_support_ai_view_kb')
    dp.callback_query.register(send_kb_copy, F.data == 'admin_support_ai_send_kb')
    dp.callback_query.register(view_ai_rules, F.data == 'admin_support_ai_view_rules')
    dp.callback_query.register(send_rules_copy, F.data == 'admin_support_ai_send_rules')
    dp.callback_query.register(show_ai_test_mode, F.data == 'admin_support_ai_test_mode')
    dp.callback_query.register(start_set_ai_test_id, F.data == 'admin_support_ai_test_set')
    dp.callback_query.register(clear_ai_test_mode, F.data == 'admin_support_ai_test_clear')
    dp.message.register(handle_new_desc, SupportSettingsStates.waiting_for_desc)
    dp.message.register(handle_ai_name, SupportSettingsStates.waiting_for_ai_name)
    dp.message.register(handle_ai_test_id, SupportSettingsStates.waiting_for_ai_test_id)
    dp.message.register(handle_kb_text, SupportSettingsStates.waiting_for_kb_text)
    dp.message.register(handle_rules_text, SupportSettingsStates.waiting_for_rules_text)
    dp.message.register(handle_sla_minutes, SupportAdvancedStates.waiting_for_sla_minutes)
    dp.message.register(handle_moderator_id, SupportAdvancedStates.waiting_for_moderator_id)
