"""
Админский хендлер для управления модулем Happ App Management.

Позволяет настраивать заголовки Happ, синхронизировать с Remnawave,
управлять провайдерами, импортировать/экспортировать конфигурацию.
"""

import json
import re
from html import escape as html_escape
from typing import Any

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.happ_management import config as cfg
from app.services.happ_management.remnawave_sync import (
    cleanup_remnawave_headers,
    schedule_sync,
    sync_to_remnawave,
)
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------


class HappManagementStates(StatesGroup):
    waiting_str_value = State()
    waiting_provider_id = State()
    waiting_import_file = State()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTITIES = {
    'announce': {
        'label': '\U0001f4e2 \u041e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u0435',
        'keys': ['ANNOUNCE_TEXT', 'ANNOUNCE_SCHEDULE_START', 'ANNOUNCE_SCHEDULE_END', 'ANNOUNCE_ONCE'],
        'section': 'client',
    },
    'info_banner': {
        'label': '\U0001fa67 \u0418\u043d\u0444\u043e-\u0431\u0430\u043d\u043d\u0435\u0440',
        'keys': ['SUB_INFO_TEXT', 'SUB_INFO_COLOR', 'SUB_INFO_BUTTON_TEXT', 'SUB_INFO_BUTTON_LINK'],
        'section': 'client',
    },
    'server_desc': {
        'label': '\U0001f4dd \u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u0441\u0435\u0440\u0432\u0435\u0440\u0430',
        'keys': ['SERVER_DESCRIPTION'],
        'section': 'client',
    },
    'color_theme': {
        'label': '\U0001f3a8 \u0422\u0435\u043c\u0430 \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\u0438\u044f (iOS)',
        'keys': ['COLOR_PROFILE'],
        'section': 'client',
    },
    'sub_expire': {
        'label': '\u23f3 \u0418\u0441\u0442\u0435\u0447\u0435\u043d\u0438\u0435 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438',
        'keys': ['SUB_EXPIRE_ENABLED', 'SUB_EXPIRE_BUTTON_LINK', 'NOTIFICATION_SUBS_EXPIRE'],
        'section': 'client',
    },
    'update_beh': {
        'label': '\U0001f504 \u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435',
        'keys': ['AUTO_UPDATE_ENABLED', 'PROFILE_UPDATE_INTERVAL', 'AUTO_UPDATE_ON_OPEN'],
        'section': 'behavior',
    },
    'autoconnect': {
        'label': '\u26a1 \u0410\u0432\u0442\u043e\u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435',
        'keys': ['AUTOCONNECT_ENABLED', 'AUTOCONNECT_TYPE'],
        'section': 'behavior',
    },
    'ping': {
        'label': '\U0001f4f6 \u041f\u0438\u043d\u0433',
        'keys': ['PING_ONOPEN_ENABLED', 'PING_TYPE', 'PING_CHECK_URL', 'PING_RESULT'],
        'section': 'behavior',
    },
    'bypass': {
        'label': '\U0001f6e1 \u041e\u0431\u0445\u043e\u0434 \u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043e\u043a',
        'keys': [
            'FRAGMENTATION_ENABLED',
            'FRAGMENTATION_PACKETS',
            'FRAGMENTATION_LENGTH',
            'FRAGMENTATION_INTERVAL',
            'FRAGMENTATION_MAXSPLIT',
            'NOISES_ENABLED',
            'NOISES_TYPE',
            'NOISES_PACKET',
            'NOISES_DELAY',
            'NOISES_APPLYTO',
            'CHANGE_USER_AGENT',
        ],
        'section': 'behavior',
    },
    'mux': {
        'label': '\U0001f310 \u041c\u0443\u043b\u044c\u0442\u0438\u043f\u043b\u0435\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435',
        'keys': ['MUX_ENABLED', 'MUX_TCP_CONNECTIONS', 'MUX_XUDP_CONNECTIONS', 'MUX_QUIC'],
        'section': 'behavior',
    },
    'security': {
        'label': '\U0001f510 \u0411\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0441\u0442\u044c',
        'keys': ['HIDE_SERVER_SETTINGS', 'ALWAYS_HWID_ENABLED', 'DISABLE_COLLAPSE'],
        'section': 'main',
    },
}

SECTION_ENTITIES = {
    'client': ['announce', 'info_banner', 'server_desc', 'color_theme', 'sub_expire'],
    'behavior': ['update_beh', 'autoconnect', 'ping', 'bypass', 'mux'],
}

BASICS_KEYS = {'MODULE_ENABLED', 'HAPP_PROVIDER_ID', 'REMNAWAVE_SYNC_ENABLED'}

PROVIDERS_PER_PAGE = 8


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _status_icon(key: str) -> str:
    """Returns a status icon based on the current value of a bool setting."""
    val = cfg.get(key)
    schema = cfg.SETTINGS_SCHEMA.get(key, {})
    if schema.get('type') == 'bool':
        return '\U0001f7e2' if val else '\u26aa\ufe0f'
    if schema.get('type') == 'choice':
        return '\U0001f535' if val else '\u26aa\ufe0f'
    if schema.get('type') == 'str':
        return '\u2705' if val else '\u2796'
    return ''


def _format_value(key: str, value: Any) -> str:
    """Форматирует значение для отображения в меню."""
    schema = cfg.SETTINGS_SCHEMA.get(key, {})
    stype = schema.get('type', 'str')
    if stype == 'bool':
        return '\U0001f7e2 Вкл' if value else '\u26aa\ufe0f Выкл'
    if stype == 'choice':
        label = cfg.get_choice_label(key, str(value) if value else '')
        return label or str(value) or '\u2014'
    if not value:
        return '\u2014 (пусто)'
    text = str(value)
    if len(text) > 40:
        text = text[:37] + '...'
    return html_escape(text)


def _entity_summary(ent_key: str) -> str:
    """Returns a one-line summary for an entity group."""
    ent = ENTITIES.get(ent_key)
    if not ent:
        return ''
    keys = ent['keys']
    active = 0
    for k in keys:
        schema = cfg.SETTINGS_SCHEMA.get(k, {})
        val = cfg.get(k)
        if (schema.get('type') == 'bool' and val) or (schema.get('type') in ('str', 'choice') and val):
            active += 1
    total = len(keys)
    return f'{active}/{total}'


def _build_main_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Главное меню Happ Management."""
    enabled = cfg.get('MODULE_ENABLED')
    sync_on = cfg.get('REMNAWAVE_SYNC_ENABLED')
    pid = cfg.get('HAPP_PROVIDER_ID')
    providers = cfg.get_providers()

    status = '\U0001f7e2 Активен' if enabled else '\U0001f534 Выключен'
    sync_status = '\U0001f7e2 Вкл' if sync_on else '\u26aa\ufe0f Выкл'

    lines = [
        '<b>\U0001f4f1 Happ App Management</b>',
        '',
        f'<b>Статус:</b> {status}',
        f'<b>Provider ID:</b> <code>{html_escape(pid)}</code>' if pid else '<b>Provider ID:</b> \u2014',
        f'<b>Синхронизация:</b> {sync_status}',
        f'<b>Провайдеров:</b> {len(providers)}' if providers else '',
    ]

    builder = InlineKeyboardBuilder()
    toggle_text = '\U0001f534 Выключить модуль' if enabled else '\U0001f7e2 Включить модуль'
    builder.row(InlineKeyboardButton(text=toggle_text, callback_data='happ_module_toggle'))
    builder.row(InlineKeyboardButton(text='\u2699\ufe0f Настройки', callback_data='happ_settings'))
    builder.row(InlineKeyboardButton(text='\U0001f310 Remnawave', callback_data='happ_remnawave'))
    builder.row(InlineKeyboardButton(text='\U0001f511 Провайдеры', callback_data='happ_providers'))
    builder.row(InlineKeyboardButton(text='\U0001f4be Бэкап', callback_data='happ_backup'))
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='admin_panel'))

    return '\n'.join(line for line in lines if line is not None), builder.as_markup()


def _build_settings_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Меню настроек — секции + быстрые переключатели."""
    lines = [
        '<b>\u2699\ufe0f Настройки Happ</b>',
        '',
    ]

    # Quick toggles for basics
    for key in ('MODULE_ENABLED', 'REMNAWAVE_SYNC_ENABLED'):
        schema = cfg.SETTINGS_SCHEMA.get(key, {})
        icon = _status_icon(key)
        lines.append(f'{icon} {schema.get("label", key)}')

    pid = cfg.get('HAPP_PROVIDER_ID')
    lines.append(
        f'\U0001f511 Provider ID: <code>{html_escape(pid)}</code>' if pid else '\U0001f511 Provider ID: \u2014'
    )

    builder = InlineKeyboardBuilder()

    # Quick toggles
    for key in ('MODULE_ENABLED', 'REMNAWAVE_SYNC_ENABLED'):
        schema = cfg.SETTINGS_SCHEMA.get(key, {})
        val = cfg.get(key)
        icon = '\U0001f7e2' if val else '\u26aa\ufe0f'
        builder.row(
            InlineKeyboardButton(
                text=f'{icon} {schema.get("label", key)}',
                callback_data=f'happ_qtoggle_{key}',
            )
        )

    # Sections
    builder.row(
        InlineKeyboardButton(text='\U0001f464 Для клиента', callback_data='happ_section_client'),
        InlineKeyboardButton(text='\u26a1 Поведение', callback_data='happ_section_behavior'),
    )

    # Entity groups from 'main' section (security)
    builder.row(
        InlineKeyboardButton(
            text=f'\U0001f510 Безопасность ({_entity_summary("security")})',
            callback_data='happ_entity_security',
        )
    )

    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_main'))

    return '\n'.join(lines), builder.as_markup()


def _build_section_menu(section_key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Меню секции (client / behavior) — список entity-групп."""
    section_label = {'client': '\U0001f464 Для клиента', 'behavior': '\u26a1 Поведение'}.get(section_key, section_key)
    lines = [f'<b>{section_label}</b>', '']

    entity_keys = SECTION_ENTITIES.get(section_key, [])
    builder = InlineKeyboardBuilder()

    for ent_key in entity_keys:
        ent = ENTITIES.get(ent_key, {})
        summary = _entity_summary(ent_key)
        builder.row(
            InlineKeyboardButton(
                text=f'{ent.get("label", ent_key)} ({summary})',
                callback_data=f'happ_entity_{ent_key}',
            )
        )

    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_settings'))

    return '\n'.join(lines), builder.as_markup()


def _build_entity_menu(entity_key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Меню настроек entity-группы (например, bypass, mux, announce)."""
    ent = ENTITIES.get(entity_key)
    if not ent:
        return 'Entity not found', InlineKeyboardMarkup(inline_keyboard=[])

    label = ent['label']
    keys = ent['keys']
    section = ent.get('section', 'client')

    lines = [f'<b>{label}</b>', '']

    for key in keys:
        schema = cfg.SETTINGS_SCHEMA.get(key, {})
        if not schema:
            continue
        val = cfg.get(key)
        dep_met = cfg.is_dependency_met(key)
        dim = '' if dep_met else '\U0001f6ab '
        lines.append(f'{dim}<b>{schema.get("label", key)}:</b> {_format_value(key, val)}')
        hint = schema.get('hint', '')
        if hint:
            lines.append(f'  <i>{hint}</i>')
        lines.append('')

    builder = InlineKeyboardBuilder()

    for key in keys:
        schema = cfg.SETTINGS_SCHEMA.get(key, {})
        if not schema:
            continue
        stype = schema.get('type', 'str')
        slabel = schema.get('label', key)

        if stype == 'bool':
            val = cfg.get(key)
            icon = '\U0001f7e2' if val else '\u26aa\ufe0f'
            builder.row(InlineKeyboardButton(text=f'{icon} {slabel}', callback_data=f'happ_toggle_{key}'))
        elif stype == 'choice':
            val = cfg.get(key)
            choice_label = cfg.get_choice_label(key, str(val) if val else '')
            builder.row(
                InlineKeyboardButton(
                    text=f'\U0001f535 {slabel}: {choice_label}',
                    callback_data=f'happ_choice_{key}',
                )
            )
        else:  # str
            builder.row(InlineKeyboardButton(text=f'\u270f\ufe0f {slabel}', callback_data=f'happ_edit_{key}'))

    # Back button
    back_data = f'happ_section_{section}' if section in SECTION_ENTITIES else 'happ_settings'
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data=back_data))

    return '\n'.join(lines), builder.as_markup()


def _build_remnawave_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Меню синхронизации с Remnawave."""
    sync_on = cfg.get('REMNAWAVE_SYNC_ENABLED')
    enabled = cfg.get('MODULE_ENABLED')

    lines = [
        '<b>\U0001f310 Remnawave</b>',
        '',
        f'<b>Модуль:</b> {"Активен" if enabled else "Выключен"}',
        f'<b>Синхронизация:</b> {"\U0001f7e2 Вкл" if sync_on else "\u26aa\ufe0f Выкл"}',
        '',
        '<i>Синхронизация отправляет Happ-заголовки в Remnawave через API.</i>',
    ]

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text='\U0001f7e2 Синхр. вкл' if sync_on else '\u26aa\ufe0f Синхр. выкл',
            callback_data='happ_qtoggle_REMNAWAVE_SYNC_ENABLED',
        )
    )
    builder.row(InlineKeyboardButton(text='\U0001f504 Синхронизировать сейчас', callback_data='happ_force_sync'))
    builder.row(InlineKeyboardButton(text='\U0001f9f9 Очистить заголовки', callback_data='happ_cleanup_remna'))
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_main'))

    return '\n'.join(lines), builder.as_markup()


def _build_backup_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Меню бэкапа настроек."""
    lines = [
        '<b>\U0001f4be Бэкап настроек</b>',
        '',
        'Экспорт — скачать все настройки и провайдеров в JSON-файл.',
        'Импорт — загрузить настройки из JSON-файла.',
    ]

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='\U0001f4e4 Экспорт', callback_data='happ_export'))
    builder.row(InlineKeyboardButton(text='\U0001f4e5 Импорт', callback_data='happ_import'))
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_main'))

    return '\n'.join(lines), builder.as_markup()


def _build_providers_menu(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Меню провайдеров с пагинацией."""
    providers = cfg.get_providers()
    total = len(providers)

    lines = [
        '<b>\U0001f511 Провайдеры</b>',
        '',
        f'Всего: {total}',
        '',
    ]

    if not providers:
        lines.append('<i>Нет добавленных провайдеров.</i>')
    else:
        start = page * PROVIDERS_PER_PAGE
        end = min(start + PROVIDERS_PER_PAGE, total)
        for i, p in enumerate(providers[start:end], start=start + 1):
            pid = p.get('provider_id', '?')
            count = p.get('total_assigned', 0)
            managed = p.get('managed', True)
            icon = '\U0001f7e2' if managed else '\u26aa\ufe0f'
            cs = p.get('custom_squad')
            squad_info = ''
            if isinstance(cs, dict) and cs.get('name'):
                squad_info = f' \u2192 {cs["name"]}'
            lines.append(f'{i}. {icon} <code>{html_escape(pid)}</code> [{count}/100]{squad_info}')

    builder = InlineKeyboardBuilder()

    if providers:
        start = page * PROVIDERS_PER_PAGE
        end = min(start + PROVIDERS_PER_PAGE, total)
        for p in providers[start:end]:
            pid = p.get('provider_id', '?')
            builder.row(
                InlineKeyboardButton(
                    text=f'\u2699\ufe0f {pid}',
                    callback_data=f'happ_pcfg_{pid}',
                )
            )

    # Pagination
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text='\u25c0\ufe0f', callback_data=f'happ_prov_pg_{page - 1}'))
    total_pages = max(1, (total + PROVIDERS_PER_PAGE - 1) // PROVIDERS_PER_PAGE)
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text='\u25b6\ufe0f', callback_data=f'happ_prov_pg_{page + 1}'))
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text='\u2795 Добавить провайдера', callback_data='happ_prov_add'))
    builder.row(InlineKeyboardButton(text='\U0001f465 Назначить пользователей', callback_data='happ_prov_assign'))
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_main'))

    return '\n'.join(lines), builder.as_markup()


def _build_provider_config_menu(provider_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Меню конфигурации конкретного провайдера."""
    providers = cfg.get_providers()
    provider = None
    for p in providers:
        if p.get('provider_id') == provider_id:
            provider = p
            break

    if not provider:
        return f'Провайдер <code>{html_escape(provider_id)}</code> не найден.', InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_providers')]]
        )

    managed = provider.get('managed', True)
    count = provider.get('total_assigned', 0)
    squad_uuid = provider.get('squad_uuid', '\u2014')
    cs = provider.get('custom_squad')
    overrides = provider.get('overrides') or {}

    lines = [
        f'<b>\u2699\ufe0f Провайдер: <code>{html_escape(provider_id)}</code></b>',
        '',
        f'<b>Managed:</b> {"\U0001f7e2 Да" if managed else "\u26aa\ufe0f Нет"}',
        f'<b>Назначено:</b> {count}/100',
        f'<b>Squad UUID:</b> <code>{html_escape(str(squad_uuid or "\u2014"))}</code>',
    ]

    if isinstance(cs, dict) and cs.get('name'):
        lines.append(f'<b>Custom Squad:</b> {html_escape(cs["name"])}')

    if overrides:
        lines.append('')
        lines.append(f'<b>Переопределений:</b> {len(overrides)}')

    builder = InlineKeyboardBuilder()
    managed_text = '\u26aa\ufe0f Снять управление' if managed else '\U0001f7e2 Управлять'
    builder.row(
        InlineKeyboardButton(
            text=managed_text,
            callback_data=f'happ_ptoggle_managed_{provider_id}',
        )
    )
    builder.row(
        InlineKeyboardButton(
            text='\U0001f5d1 Удалить провайдера',
            callback_data=f'happ_pdel_{provider_id}',
        )
    )
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_providers'))

    return '\n'.join(lines), builder.as_markup()


def _build_choice_menu(key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Меню выбора значения для choice-параметра."""
    schema = cfg.SETTINGS_SCHEMA.get(key, {})
    choices = schema.get('choices', [])
    current = cfg.get(key)

    lines = [
        f'<b>{schema.get("label", key)}</b>',
        '',
        f'<i>{schema.get("hint", "")}</i>',
        '',
        f'Текущее: <b>{cfg.get_choice_label(key, str(current) if current else "")}</b>',
    ]

    builder = InlineKeyboardBuilder()
    for ch in choices:
        label = cfg.get_choice_label(key, ch)
        icon = '\U0001f535' if str(current) == str(ch) else '\u26aa\ufe0f'
        builder.row(
            InlineKeyboardButton(
                text=f'{icon} {label}',
                callback_data=f'happ_set_choice_{key}_{ch}',
            )
        )

    # Back: figure out which entity this key belongs to
    back_data = _back_data_for_key(key)
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data=back_data))

    return '\n'.join(lines), builder.as_markup()


def _back_data_for_key(key: str) -> str:
    """Определяет callback_data кнопки 'Назад' для ключа настройки."""
    for ent_key, ent in ENTITIES.items():
        if key in ent['keys']:
            return f'happ_entity_{ent_key}'
    return 'happ_settings'


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@admin_required
@error_handler
async def show_happ_main(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    text, markup = _build_main_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    text, markup = _build_settings_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_section(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    section_key = callback.data.replace('happ_section_', '', 1)
    if section_key not in SECTION_ENTITIES:
        await callback.answer('Секция не найдена', show_alert=True)
        return
    text, markup = _build_section_menu(section_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_entity(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    entity_key = callback.data.replace('happ_entity_', '', 1)
    if entity_key not in ENTITIES:
        await callback.answer('Группа не найдена', show_alert=True)
        return
    text, markup = _build_entity_menu(entity_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_remnawave(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    text, markup = _build_remnawave_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_backup(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    text, markup = _build_backup_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_providers(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    text, markup = _build_providers_menu(page=0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def providers_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    page_str = callback.data.replace('happ_prov_pg_', '', 1)
    try:
        page = int(page_str)
    except ValueError:
        page = 0
    text, markup = _build_providers_menu(page=page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def toggle_module(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    current = cfg.get('MODULE_ENABLED')
    cfg.set_value('MODULE_ENABLED', not current)
    schedule_sync()
    text, markup = _build_main_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    status = 'включён' if not current else 'выключен'
    await callback.answer(f'Модуль {status}')


@admin_required
@error_handler
async def quick_toggle(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_qtoggle_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema or schema.get('type') != 'bool':
        await callback.answer('Параметр не найден', show_alert=True)
        return

    current = cfg.get(key)
    cfg.set_value(key, not current)
    schedule_sync()

    label = schema.get('label', key)
    status = 'включён' if not current else 'выключен'

    # Rebuild the menu the user was in
    if callback.data.endswith('REMNAWAVE_SYNC_ENABLED'):
        # Could be from settings or remnawave menu
        text, markup = _build_settings_menu()
    else:
        text, markup = _build_settings_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer(f'{label}: {status}')


@admin_required
@error_handler
async def toggle_bool(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_toggle_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema or schema.get('type') != 'bool':
        await callback.answer('Параметр не найден', show_alert=True)
        return

    current = cfg.get(key)
    cfg.set_value(key, not current)

    # If toggling ANNOUNCE_TEXT related bools and text was cleared
    if key == 'ANNOUNCE_ONCE' and not current and cfg.get('ANNOUNCE_TEXT'):
        pass  # Will be consumed on next sync

    schedule_sync()

    # Rebuild entity menu
    entity_key = None
    for ent_key, ent in ENTITIES.items():
        if key in ent['keys']:
            entity_key = ent_key
            break

    if entity_key:
        text, markup = _build_entity_menu(entity_key)
    else:
        text, markup = _build_settings_menu()

    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    label = schema.get('label', key)
    status = 'включён' if not current else 'выключен'
    await callback.answer(f'{label}: {status}')


@admin_required
@error_handler
async def show_choice(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_choice_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema or schema.get('type') != 'choice':
        await callback.answer('Параметр не найден', show_alert=True)
        return
    text, markup = _build_choice_menu(key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def set_choice(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    # Format: happ_set_choice_{KEY}_{VALUE}
    # KEY can contain underscores, VALUE can be empty
    raw = callback.data.replace('happ_set_choice_', '', 1)
    # Find the key by checking against known schema keys
    found_key = None
    found_value = None
    for schema_key in cfg.SETTINGS_SCHEMA:
        prefix = schema_key + '_'
        if raw == schema_key:
            found_key = schema_key
            found_value = ''
            break
        if raw.startswith(prefix):
            found_key = schema_key
            found_value = raw[len(prefix) :]
            break
    if not found_key:
        await callback.answer('Параметр не найден', show_alert=True)
        return

    cfg.set_value(found_key, found_value)
    schedule_sync()

    label = cfg.get_choice_label(found_key, found_value)
    text, markup = _build_choice_menu(found_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer(f'Установлено: {label}')


@admin_required
@error_handler
async def start_edit_str(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_edit_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema:
        await callback.answer('Параметр не найден', show_alert=True)
        return

    current = cfg.get(key)
    lines = [
        f'<b>\u270f\ufe0f {schema.get("label", key)}</b>',
        '',
        f'<i>{schema.get("hint", "")}</i>',
        '',
    ]
    if current:
        lines.append(f'Текущее: <code>{html_escape(str(current))}</code>')
    else:
        lines.append('Текущее: \u2014 (пусто)')
    lines.append('')
    lines.append('Отправьте новое значение или /cancel для отмены.')

    builder = InlineKeyboardBuilder()
    if current:
        builder.row(InlineKeyboardButton(text='\U0001f5d1 Очистить', callback_data=f'happ_clear_{key}'))
    back_data = _back_data_for_key(key)
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data=back_data))

    await state.set_state(HappManagementStates.waiting_str_value)
    await state.update_data(happ_edit_key=key)

    await callback.message.edit_text('\n'.join(lines), reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def process_str_value(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    key = data.get('happ_edit_key')
    if not key:
        await state.clear()
        return

    if message.text and message.text.strip() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return

    value = (message.text or '').strip()
    schema = cfg.SETTINGS_SCHEMA.get(key, {})

    # Validate
    error = cfg.validate_value(key, value)
    if error:
        await message.answer(f'\u274c {error}\n\nПопробуйте снова или /cancel.', parse_mode='HTML')
        return

    cfg.set_value(key, value)
    schedule_sync()
    await state.clear()

    label = schema.get('label', key)
    if value:
        await message.answer(
            f'\u2705 <b>{label}</b> установлено:\n<code>{html_escape(value[:200])}</code>',
            parse_mode='HTML',
        )
    else:
        await message.answer(f'\u2705 <b>{label}</b> очищено.', parse_mode='HTML')


@admin_required
@error_handler
async def clear_str_value(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_clear_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key, {})

    # If clearing announce text, mark for clear in remnawave
    if key == 'ANNOUNCE_TEXT' and cfg.get('ANNOUNCE_TEXT'):
        cfg.mark_announce_clear()

    cfg.set_value(key, schema.get('default', ''))
    schedule_sync()
    await state.clear()

    # Return to entity menu
    entity_key = None
    for ent_key, ent in ENTITIES.items():
        if key in ent['keys']:
            entity_key = ent_key
            break

    if entity_key:
        text, markup = _build_entity_menu(entity_key)
        await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    label = schema.get('label', key)
    await callback.answer(f'{label} очищено')


@admin_required
@error_handler
async def force_sync(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if not cfg.get('REMNAWAVE_SYNC_ENABLED'):
        await callback.answer('Синхронизация выключена. Включите в настройках.', show_alert=True)
        return

    await callback.answer('Синхронизация запущена...')

    try:
        success, total = await sync_to_remnawave()
        result_text = f'\u2705 Синхронизация: {success}/{total}'
        if success < total:
            result_text = f'\u26a0\ufe0f Синхронизация: {success}/{total} (есть ошибки)'
    except Exception as e:
        logger.error(f'[HappHandler] force_sync error: {e}', exc_info=True)
        result_text = f'\u274c Ошибка синхронизации: {e}'

    text, markup = _build_remnawave_menu()
    lines = text.split('\n')
    lines.append('')
    lines.append(f'<b>Результат:</b> {result_text}')
    await callback.message.edit_text('\n'.join(lines), reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def cleanup_remna(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await callback.answer('Очистка заголовков...')

    try:
        success, total = await cleanup_remnawave_headers()
        result_text = f'\u2705 Очищено: {success}/{total}'
        if success < total:
            result_text = f'\u26a0\ufe0f Очищено: {success}/{total} (есть ошибки)'
    except Exception as e:
        logger.error(f'[HappHandler] cleanup error: {e}', exc_info=True)
        result_text = f'\u274c Ошибка: {e}'

    text, markup = _build_remnawave_menu()
    lines = text.split('\n')
    lines.append('')
    lines.append(f'<b>Результат:</b> {result_text}')
    await callback.message.edit_text('\n'.join(lines), reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def start_add_provider(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(HappManagementStates.waiting_provider_id)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Отмена', callback_data='happ_providers'))

    await callback.message.edit_text(
        '<b>\u2795 Добавление провайдера</b>\n\n'
        'Отправьте Provider ID (например: <code>nS5jOH5b</code>).\n'
        "Получите его на <a href='https://happ-proxy.com'>happ-proxy.com</a>.\n\n"
        'Или /cancel для отмены.',
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def process_add_provider(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if message.text and message.text.strip() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return

    provider_id = (message.text or '').strip()
    if not provider_id:
        await message.answer('\u274c Пустой Provider ID. Попробуйте снова или /cancel.')
        return

    if not re.match(r'^[a-zA-Z0-9_-]+$', provider_id):
        await message.answer(
            '\u274c Provider ID может содержать только латинские буквы, цифры, - и _.\nПопробуйте снова или /cancel.'
        )
        return

    added = cfg.add_provider(provider_id)
    await state.clear()

    if added:
        schedule_sync()
        await message.answer(
            f'\u2705 Провайдер <code>{html_escape(provider_id)}</code> добавлен.\n'
            'Сквад будет создан при следующей синхронизации.',
            parse_mode='HTML',
        )
    else:
        await message.answer(
            f'\u26a0\ufe0f Провайдер <code>{html_escape(provider_id)}</code> уже существует.',
            parse_mode='HTML',
        )


@admin_required
@error_handler
async def show_provider_config(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    provider_id = callback.data.replace('happ_pcfg_', '', 1)
    text, markup = _build_provider_config_menu(provider_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def toggle_provider_managed(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    provider_id = callback.data.replace('happ_ptoggle_managed_', '', 1)
    current = cfg.is_provider_managed(provider_id)
    cfg.set_provider_managed(provider_id, not current)
    schedule_sync()

    text, markup = _build_provider_config_menu(provider_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    status = 'включено' if not current else 'выключено'
    await callback.answer(f'Управление заголовками: {status}')


@admin_required
@error_handler
async def delete_provider(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    provider_id = callback.data.replace('happ_pdel_', '', 1)
    removed = cfg.remove_provider(provider_id)

    if removed:
        schedule_sync()
        await callback.answer(f'Провайдер {provider_id} удалён')
    else:
        await callback.answer('Провайдер не найден', show_alert=True)

    text, markup = _build_providers_menu(page=0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def assign_users_now(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    providers = cfg.get_providers()
    if not providers:
        await callback.answer('Нет провайдеров', show_alert=True)
        return

    if not cfg.get('REMNAWAVE_SYNC_ENABLED'):
        await callback.answer('Синхронизация выключена', show_alert=True)
        return

    await callback.answer('Назначение пользователей запущено...')

    try:
        from app.services.happ_management.squad_manager import run_periodic_assignment

        assigned = await run_periodic_assignment()
        result = f'\u2705 Назначено: {assigned}'
    except Exception as e:
        logger.error(f'[HappHandler] assign error: {e}', exc_info=True)
        result = f'\u274c Ошибка: {e}'

    text, markup = _build_providers_menu(page=0)
    lines = text.split('\n')
    lines.append('')
    lines.append(f'<b>Результат:</b> {result}')
    await callback.message.edit_text('\n'.join(lines), reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def export_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    try:
        data = cfg.export_all()
        content = json.dumps(data, ensure_ascii=False, indent=2)
        file = BufferedInputFile(
            content.encode('utf-8'),
            filename='happ_management_backup.json',
        )
        await callback.message.answer_document(file, caption='\U0001f4be Бэкап настроек Happ Management')
        await callback.answer()
    except Exception as e:
        logger.error(f'[HappHandler] export error: {e}', exc_info=True)
        await callback.answer(f'Ошибка экспорта: {e}', show_alert=True)


@admin_required
@error_handler
async def start_import(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(HappManagementStates.waiting_import_file)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Отмена', callback_data='happ_backup'))

    await callback.message.edit_text(
        '<b>\U0001f4e5 Импорт настроек</b>\n\nОтправьте JSON-файл с бэкапом настроек.\n\nИли /cancel для отмены.',
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_import_file(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if message.text and message.text.strip() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return

    if not message.document:
        await message.answer('\u274c Отправьте JSON-файл или /cancel.')
        return

    try:
        file = await message.bot.download(message.document)
        content = file.read().decode('utf-8')
        data = json.loads(content)
    except json.JSONDecodeError:
        await message.answer('\u274c Невалидный JSON. Попробуйте другой файл или /cancel.')
        return
    except Exception as e:
        await message.answer(f'\u274c Ошибка чтения файла: {e}')
        return

    if not isinstance(data, dict):
        await message.answer('\u274c Файл должен содержать JSON-объект.')
        return

    try:
        s_count, p_count = cfg.import_all(data)
        schedule_sync()
        await state.clear()
        await message.answer(
            f'\u2705 Импорт завершён!\nНастроек: {s_count}\nПровайдеров: {p_count}',
            parse_mode='HTML',
        )
    except Exception as e:
        logger.error(f'[HappHandler] import error: {e}', exc_info=True)
        await message.answer(f'\u274c Ошибка импорта: {e}')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_handlers(dp: Dispatcher):
    # Main menu
    dp.callback_query.register(show_happ_main, F.data == 'happ_main')

    # Settings
    dp.callback_query.register(show_happ_settings, F.data == 'happ_settings')
    dp.callback_query.register(show_happ_section, F.data.startswith('happ_section_'))
    dp.callback_query.register(show_happ_entity, F.data.startswith('happ_entity_'))

    # Remnawave
    dp.callback_query.register(show_happ_remnawave, F.data == 'happ_remnawave')

    # Backup
    dp.callback_query.register(show_happ_backup, F.data == 'happ_backup')

    # Providers
    dp.callback_query.register(show_happ_providers, F.data == 'happ_providers')
    dp.callback_query.register(providers_page, F.data.startswith('happ_prov_pg_'))
    dp.callback_query.register(show_provider_config, F.data.startswith('happ_pcfg_'))
    dp.callback_query.register(toggle_provider_managed, F.data.startswith('happ_ptoggle_managed_'))
    dp.callback_query.register(delete_provider, F.data.startswith('happ_pdel_'))

    # Toggles
    dp.callback_query.register(toggle_module, F.data == 'happ_module_toggle')
    dp.callback_query.register(quick_toggle, F.data.startswith('happ_qtoggle_'))
    dp.callback_query.register(toggle_bool, F.data.startswith('happ_toggle_'))

    # Choices
    dp.callback_query.register(show_choice, F.data.startswith('happ_choice_'))
    dp.callback_query.register(set_choice, F.data.startswith('happ_set_choice_'))

    # String editing
    dp.callback_query.register(start_edit_str, F.data.startswith('happ_edit_'))
    dp.message.register(process_str_value, StateFilter(HappManagementStates.waiting_str_value))
    dp.callback_query.register(clear_str_value, F.data.startswith('happ_clear_'))

    # Remnawave actions
    dp.callback_query.register(force_sync, F.data == 'happ_force_sync')
    dp.callback_query.register(cleanup_remna, F.data == 'happ_cleanup_remna')

    # Provider actions
    dp.callback_query.register(start_add_provider, F.data == 'happ_prov_add')
    dp.message.register(process_add_provider, StateFilter(HappManagementStates.waiting_provider_id))
    dp.callback_query.register(assign_users_now, F.data == 'happ_prov_assign')

    # Backup actions
    dp.callback_query.register(export_settings, F.data == 'happ_export')
    dp.callback_query.register(start_import, F.data == 'happ_import')
    dp.message.register(process_import_file, StateFilter(HappManagementStates.waiting_import_file))
