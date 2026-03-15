"""
Хранилище настроек Happ App Management.
Настройки хранятся в JSON-файле и применяются без перезагрузки бота.
"""

import json
import os
import re
from typing import Any

import structlog


logger = structlog.get_logger(__name__)

SETTINGS_FILE = 'data/happ_management.json'

SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    # -- Модуль --
    'MODULE_ENABLED': {
        'type': 'bool',
        'default': True,
        'label': 'Модуль включён',
        'category': 'main',
        'hint': 'Выключите, если подписка не обновляется — модуль перестанет добавлять заголовки. Используйте для диагностики.',
    },
    'REMNAWAVE_SYNC_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Синхронизация с Remnawave',
        'category': 'main',
        'hint': 'Автоматически отправляет Happ-заголовки в Remnawave через API. Включите, если используете Remnawave — старые подписки заработают без перевыпуска.',
    },
    # -- Идентификация --
    'HAPP_PROVIDER_ID': {
        'type': 'str',
        'default': '',
        'label': 'Provider ID',
        'category': 'provider',
        'hint': 'Обязателен для работы расширенных параметров. Получите на happ-proxy.com и привяжите свой домен.',
    },
    'REASSIGN_FROM_FOREIGN_SQUADS': {
        'type': 'bool',
        'default': False,
        'label': 'Забирать из чужих сквадов',
        'category': 'provider',
        'hint': 'Если включено — модуль перетянет пользователей из чужих сквадов. Если задан список источников (в разделе Remnawave) — только из указанных сквадов. Если список пуст — из всех подряд.',
    },
    'SUBSCRIPTION_DOMAIN': {
        'type': 'str',
        'default': '',
        'label': 'Домен подписки',
        'category': 'provider',
        'hint': 'Домен, через который клиенты получают подписку (например mydomain.com). Используется для привязки к happ-proxy.com при авторегистрации.',
    },
    'CAPTCHA_API_KEY': {
        'type': 'str',
        'default': '',
        'label': 'API-ключ капчи (rucaptcha/2captcha)',
        'category': 'provider',
        'hint': 'Ключ от rucaptcha.com или 2captcha.com. Нужен для HTTP-метода авторегистрации (без браузера).',
    },
    'AUTOREG_METHOD': {
        'type': 'str',
        'default': 'auto',
        'label': 'Метод авторегистрации',
        'category': 'provider',
        'hint': 'auto — выберет лучший доступный; http — быстрый через rucaptcha (нужен API-ключ); nodriver — бесплатный через браузер.',
    },
    # -- Безопасность --
    'HIDE_SERVER_SETTINGS': {
        'type': 'bool',
        'default': False,
        'label': 'Скрыть настройки серверов',
        'category': 'security',
        'hint': 'Клиент не сможет видеть IP, порты и протоколы серверов. Рекомендуется включить.',
    },
    'ALWAYS_HWID_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Неотключаемый HWID',
        'category': 'security',
        'hint': 'Клиент не сможет отключить передачу отпечатка устройства. Включайте, если ограничиваете кол-во устройств на подписку.',
    },
    'DISABLE_COLLAPSE': {
        'type': 'bool',
        'default': False,
        'label': 'Запретить сворачивание подписки',
        'category': 'security',
        'hint': 'Отключает возможность сворачивания подписки в приложении. Подписка всегда будет развёрнута.',
    },
    # -- Уведомления --
    'SUB_EXPIRE_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Баннер «Подписка заканчивается»',
        'category': 'notifications',
        'hint': 'Баннер в приложении за 3 дня до окончания. Рекомендуется включить — клиенты будут продлевать вовремя.',
        'group': 'Баннер истечения',
    },
    'SUB_EXPIRE_BUTTON_LINK': {
        'type': 'str',
        'default': '',
        'label': 'Ссылка кнопки «Продлить»',
        'category': 'notifications',
        'hint': 'Ссылка для кнопки в баннере истечения. Пусто = кнопка не показывается. Пример: https://t.me/your_bot',
        'group': 'Баннер истечения',
        'depends_on': 'SUB_EXPIRE_ENABLED',
    },
    'NOTIFICATION_SUBS_EXPIRE': {
        'type': 'bool',
        'default': False,
        'label': 'Push за 3 дня до истечения',
        'category': 'notifications',
        'hint': 'Push-уведомление на телефон за 3 дня до конца подписки. 1 раз в день, 3 дня подряд. Рекомендуется включить.',
    },
    # -- Внешний вид --
    'SUB_INFO_TEXT': {
        'type': 'str',
        'default': '',
        'label': 'Текст инфо-баннера',
        'category': 'appearance',
        'hint': 'Произвольный текст внутри приложения (до 200 символов). Показывается когда нет баннера об истечении. Пусто = не показывается.',
        'max_length': 200,
        'group': 'Инфо-баннер',
    },
    'SUB_INFO_COLOR': {
        'type': 'choice',
        'default': 'blue',
        'choices': ['red', 'blue', 'green'],
        'label': 'Цвет инфо-баннера',
        'category': 'appearance',
        'hint': 'Цвет рамки информационного баннера.',
        'group': 'Инфо-баннер',
    },
    'SUB_INFO_BUTTON_TEXT': {
        'type': 'str',
        'default': '',
        'label': 'Текст кнопки',
        'category': 'appearance',
        'hint': 'Текст кнопки в инфо-баннере (до 25 символов). Пусто = кнопка не показывается.',
        'max_length': 25,
        'group': 'Инфо-баннер',
    },
    'SUB_INFO_BUTTON_LINK': {
        'type': 'str',
        'default': '',
        'label': 'Ссылка кнопки баннера',
        'category': 'appearance',
        'hint': 'URL или deeplink для кнопки инфо-баннера.',
        'group': 'Инфо-баннер',
    },
    'ANNOUNCE_TEXT': {
        'type': 'str',
        'default': '',
        'label': 'Текст объявления',
        'category': 'appearance',
        'hint': 'Текст внутри подписки в красной рамке (до 200 символов). Пусто = стандартное объявление бота. Поддерживает эмодзи.',
        'max_length': 200,
        'group': 'Объявление',
    },
    'ANNOUNCE_SCHEDULE_START': {
        'type': 'str',
        'default': '',
        'label': 'Начало показа',
        'category': 'appearance',
        'hint': 'С какого времени показывать объявление (формат ЧЧ:ММ, например 09:00). Пусто = показывать всегда.',
        'group': 'Объявление',
        'depends_on': 'ANNOUNCE_TEXT',
        'validate': r'^([01]\d|2[0-3]):[0-5]\d$',
        'validate_hint': 'Формат: HH:MM (например, 09:00)',
    },
    'ANNOUNCE_SCHEDULE_END': {
        'type': 'str',
        'default': '',
        'label': 'Конец показа',
        'category': 'appearance',
        'hint': 'До какого времени показывать объявление (формат ЧЧ:ММ, например 18:00). Пусто = показывать всегда.',
        'group': 'Объявление',
        'depends_on': 'ANNOUNCE_TEXT',
        'validate': r'^([01]\d|2[0-3]):[0-5]\d$',
        'validate_hint': 'Формат: HH:MM (например, 18:00)',
    },
    'ANNOUNCE_ONCE': {
        'type': 'bool',
        'default': False,
        'label': 'Показать один раз',
        'category': 'appearance',
        'hint': 'Объявление покажется пользователям и автоматически удалится.',
        'group': 'Объявление',
        'depends_on': 'ANNOUNCE_TEXT',
    },
    'SERVER_DESCRIPTION': {
        'type': 'str',
        'default': '',
        'label': 'Описание сервера',
        'category': 'appearance',
        'hint': 'Текст под названием каждого сервера в приложении (до 30 символов). Пусто = не используется.',
        'max_length': 30,
    },
    'COLOR_PROFILE': {
        'type': 'str',
        'default': '',
        'label': 'Тема оформления (iOS)',
        'category': 'appearance',
        'hint': 'Только iOS. Как получить: Happ → Настройки → удерживайте «Тема оформления» → экспорт. Вставьте JSON сюда — он минифицируется автоматически. Цвета можно менять вручную перед вставкой.',
        'warning': True,
        'long_hint': True,
    },
    # -- Обновление --
    'AUTO_UPDATE_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Автообновление подписки',
        'category': 'update',
        'hint': 'Приложение само скачивает свежий список серверов по расписанию. Рекомендуется включить.',
    },
    'PROFILE_UPDATE_INTERVAL': {
        'type': 'str',
        'default': '3',
        'label': 'Интервал обновления (часы)',
        'category': 'behavior',
        'hint': 'Как часто приложение обновляет список серверов (в часах, кратно 1). По умолчанию: 3.',
        'validate': r'^\d+$',
        'validate_hint': 'Допустимо: целое число (часы)',
        'validate_range': (1, 24),
        'depends_on': 'AUTO_UPDATE_ENABLED',
    },
    'AUTO_UPDATE_ON_OPEN': {
        'type': 'bool',
        'default': False,
        'label': 'Обновлять при открытии',
        'category': 'behavior',
        'hint': 'При каждом открытии приложения автоматически обновляются ВСЕ подписки. Решает проблему «открыл — визуала нет».',
        'depends_on': 'AUTO_UPDATE_ENABLED',
    },
    # -- Обход блокировок --
    'FRAGMENTATION_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Фрагментация (обход DPI)',
        'category': 'bypass',
        'hint': 'Разбивает пакеты на мелкие части, чтобы провайдер не распознал VPN (обход DPI).',
        'warning': True,
        'group': 'Фрагментация',
    },
    'FRAGMENTATION_PACKETS': {
        'type': 'str',
        'default': 'tlshello',
        'label': 'Пакеты для фрагментации',
        'category': 'bypass',
        'hint': 'Рекомендуемое: tlshello. Другие варианты: 1-2, 1-3, 1-5.',
        'validate': r'^(tlshello|\d+-\d+)$',
        'validate_hint': 'Допустимо: tlshello или диапазон (например, 1-3)',
        'group': 'Фрагментация',
        'depends_on': 'FRAGMENTATION_ENABLED',
    },
    'FRAGMENTATION_LENGTH': {
        'type': 'str',
        'default': '50-100',
        'label': 'Размер фрагментов',
        'category': 'bypass',
        'hint': 'Рекомендуемое: 50-100 (байт). Диапазон через дефис.',
        'validate': r'^\d+-\d+$',
        'validate_hint': 'Допустимо: диапазон через дефис (например, 50-100)',
        'group': 'Фрагментация',
        'depends_on': 'FRAGMENTATION_ENABLED',
    },
    'FRAGMENTATION_INTERVAL': {
        'type': 'str',
        'default': '5',
        'label': 'Задержка фрагментации (мс)',
        'category': 'bypass',
        'hint': 'Рекомендуемое: 5 (мс). Можно указать диапазон, например 5-10.',
        'validate': r'^\d+(-\d+)?$',
        'validate_hint': 'Допустимо: число или диапазон (например, 5 или 5-10)',
        'group': 'Фрагментация',
        'depends_on': 'FRAGMENTATION_ENABLED',
    },
    'FRAGMENTATION_MAXSPLIT': {
        'type': 'str',
        'default': '',
        'label': 'Макс. кол-во фрагментов',
        'category': 'bypass',
        'hint': 'Максимальное количество частей при разбиении. Диапазон через дефис. Пусто = по умолчанию. Пример: 100-200.',
        'validate': r'^\d+(-\d+)?$',
        'validate_hint': 'Допустимо: число или диапазон (например, 100-200)',
        'group': 'Фрагментация',
        'depends_on': 'FRAGMENTATION_ENABLED',
    },
    'NOISES_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Шум (маскировка VPN)',
        'category': 'bypass',
        'hint': 'Добавляет мусорный трафик для маскировки. Используется вместе с фрагментацией.',
        'warning': True,
        'group': 'Шум',
    },
    'NOISES_TYPE': {
        'type': 'str',
        'default': 'rand',
        'label': 'Тип шума',
        'category': 'bypass',
        'hint': 'Рекомендуемое: rand (случайный). Другие: str, base64.',
        'validate': r'^(rand|str|base64)$',
        'validate_hint': 'Допустимо: rand, str или base64',
        'group': 'Шум',
        'depends_on': 'NOISES_ENABLED',
    },
    'NOISES_PACKET': {
        'type': 'str',
        'default': '10-20',
        'label': 'Размер шумовых пакетов',
        'category': 'bypass',
        'hint': 'Рекомендуемое: 10-20 (байт). Диапазон через дефис.',
        'validate': r'^\d+-\d+$',
        'validate_hint': 'Допустимо: диапазон через дефис (например, 10-20)',
        'group': 'Шум',
        'depends_on': 'NOISES_ENABLED',
    },
    'NOISES_DELAY': {
        'type': 'str',
        'default': '10-16',
        'label': 'Задержка шума (мс)',
        'category': 'bypass',
        'hint': 'Рекомендуемое: 10-16 (мс). Диапазон через дефис.',
        'validate': r'^\d+-\d+$',
        'validate_hint': 'Допустимо: диапазон через дефис (например, 10-16)',
        'group': 'Шум',
        'depends_on': 'NOISES_ENABLED',
    },
    'NOISES_APPLYTO': {
        'type': 'choice',
        'default': '',
        'choices': ['', 'ip', 'ipv4', 'ipv6'],
        'label': 'Шум: применять к',
        'category': 'bypass',
        'hint': 'К какому типу трафика применять шум. Пусто = по умолчанию (все). ipv4/ipv6 = только указанная версия IP.',
        'group': 'Шум',
        'depends_on': 'NOISES_ENABLED',
    },
    'CHANGE_USER_AGENT': {
        'type': 'str',
        'default': '',
        'label': 'Подмена User-Agent',
        'category': 'bypass',
        'hint': 'Happ будет притворяться браузером при запросе подписки. Нужно вставить полную строку, например: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0. Пусто = стандартный (Happ/1.0).',
        'warning': True,
    },
    # -- Поведение приложения --
    'AUTOCONNECT_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Автоподключение',
        'category': 'behavior',
        'hint': 'При запуске приложение автоматически подключается к серверу.',
        'group': 'Автоподключение',
    },
    'AUTOCONNECT_TYPE': {
        'type': 'choice',
        'default': 'lastused',
        'choices': ['lastused', 'lowestdelay'],
        'label': 'Режим автоподключения',
        'category': 'behavior',
        'hint': 'lastused = последний сервер. lowestdelay = сервер с минимальным пингом.',
        'group': 'Автоподключение',
        'depends_on': 'AUTOCONNECT_ENABLED',
    },
    'PING_ONOPEN_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Пинг при открытии',
        'category': 'behavior',
        'hint': 'Автоматически тестирует все серверы при открытии приложения.',
        'group': 'Пинг',
    },
    'PING_TYPE': {
        'type': 'choice',
        'default': '',
        'choices': ['', 'proxy', 'proxy-head', 'tcp', 'icmp'],
        'label': 'Тип пинга',
        'category': 'behavior',
        'hint': 'proxy = через VPN (точнее). tcp = TCP-пинг. icmp = ICMP-пинг. Пусто = по умолчанию.',
        'group': 'Пинг',
    },
    'PING_CHECK_URL': {
        'type': 'str',
        'default': '',
        'label': 'URL для proxy-пинга',
        'category': 'behavior',
        'hint': 'URL проверки для ping-type=proxy. Пусто = по умолчанию. Пример: https://cp.cloudflare.com/generate_204',
        'group': 'Пинг',
        'depends_on': 'PING_TYPE',
    },
    'PING_RESULT': {
        'type': 'choice',
        'default': '',
        'choices': ['', 'time', 'icon'],
        'label': 'Отображение пинга',
        'category': 'behavior',
        'hint': 'time = число в мс. icon = цветная иконка. Пусто = по умолчанию.',
        'group': 'Пинг',
    },
    # -- Сеть --
    'MUX_ENABLED': {
        'type': 'bool',
        'default': False,
        'label': 'Мультиплексирование (Mux)',
        'category': 'network',
        'hint': 'Несколько соединений через один канал. Ускоряет открытие страниц, но может замедлить скачивание файлов.',
        'warning': True,
        'group': 'Мультиплексирование',
    },
    'MUX_TCP_CONNECTIONS': {
        'type': 'str',
        'default': '8',
        'label': 'TCP-соединений (Mux)',
        'category': 'network',
        'hint': 'Рекомендуемое: 8. Допустимо: от 1 до 1024.',
        'validate': r'^\d+$',
        'validate_hint': 'Допустимо: целое число от 1 до 1024',
        'validate_range': (1, 1024),
        'group': 'Мультиплексирование',
        'depends_on': 'MUX_ENABLED',
    },
    'MUX_XUDP_CONNECTIONS': {
        'type': 'str',
        'default': '8',
        'label': 'xUDP-соединений (Mux)',
        'category': 'network',
        'hint': 'Рекомендуемое: 8. Допустимо: от 1 до 1024.',
        'validate': r'^\d+$',
        'validate_hint': 'Допустимо: целое число от 1 до 1024',
        'validate_range': (1, 1024),
        'group': 'Мультиплексирование',
        'depends_on': 'MUX_ENABLED',
    },
    'MUX_QUIC': {
        'type': 'choice',
        'default': '',
        'choices': ['', 'skip', 'allow', 'reject'],
        'label': 'Mux: QUIC-трафик',
        'category': 'network',
        'hint': 'Обработка QUIC-трафика при мультиплексировании. skip = пропускать без мультиплексирования. Пусто = по умолчанию.',
        'group': 'Мультиплексирование',
        'depends_on': 'MUX_ENABLED',
    },
}

# Подсказки для категорий (показываются вверху меню категории)
CATEGORY_HINTS = {
    'main': 'Если подписка в Happ не обновляется — выключите модуль. Если после выключения заработает, проблема в заголовках или Provider ID (проверьте привязку домена на happ-proxy.com).',
    'provider': 'Provider ID необходим для работы всех расширенных параметров. Получите его на happ-proxy.com и привяжите свой домен подписки.',
    'security': 'Эти параметры защищают конфигурацию серверов от просмотра и изменения клиентом.',
    'notifications': 'Push-уведомления и баннер об истечении подписки.',
    'appearance': 'Объявления, инфо-баннеры, описание серверов и тема оформления.',
    'update': 'Управление автоматическим обновлением списка серверов.',
    'behavior': 'Автоподключение, тестирование серверов и отображение пинга.',
    'bypass': '⚠️ <b>Внимание:</b> эти параметры влияют на сетевое поведение приложения. Включайте только если ваш провайдер блокирует VPN-трафик. Если не понимаете как это работает — не включайте.',
    'network': '⚠️ <b>Внимание:</b> мультиплексирование — продвинутая настройка. Включайте только если клиенты жалуются на медленное открытие сайтов. Может замедлить скачивание файлов. Если не уверены — не включайте.',
}

CATEGORIES = {
    'main': '⚙️ Модуль',
    'provider': '🔑 Провайдер',
    'security': '🔐 Безопасность',
    'notifications': '🔔 Уведомления',
    'appearance': '🎨 Внешний вид',
    'update': '🔄 Обновление',
    'behavior': '📱 Поведение',
    'bypass': '🛡 Обход блокировок',
    'network': '🌐 Сеть',
}

CATEGORY_ORDER = [
    'main',
    'provider',
    'security',
    'notifications',
    'appearance',
    'update',
    'behavior',
    'bypass',
    'network',
]

SECTIONS = {
    'basics': {
        'label': '⚙️ Настройки модуля',
        'categories': ['main', 'provider', 'update', 'security'],
    },
    'client': {
        'label': '👤 Для клиента',
        'categories': ['appearance', 'notifications'],
    },
    'behavior': {
        'label': '⚡ Поведение',
        'categories': ['behavior', 'bypass', 'network'],
    },
}

SECTION_ORDER = ['basics', 'client', 'behavior']

CATEGORY_TO_SECTION = {
    'main': 'basics',
    'provider': 'basics',
    'update': 'basics',
    'security': 'basics',
    'notifications': 'client',
    'appearance': 'client',
    'behavior': 'behavior',
    'bypass': 'behavior',
    'network': 'behavior',
}

CHOICE_LABELS: dict[str, dict[str, str]] = {
    'AUTOCONNECT_TYPE': {
        'lastused': 'Последний сервер',
        'lowestdelay': 'Минимальный пинг',
    },
    'PING_TYPE': {
        '': 'По умолчанию',
        'proxy': 'Proxy',
        'proxy-head': 'Proxy HEAD',
        'tcp': 'TCP',
        'icmp': 'ICMP',
    },
    'PING_RESULT': {
        '': 'По умолчанию',
        'time': 'Время (мс)',
        'icon': 'Иконка',
    },
    'SUB_INFO_COLOR': {
        'red': 'Красный',
        'blue': 'Синий',
        'green': 'Зелёный',
    },
    'NOISES_APPLYTO': {
        '': 'По умолчанию',
        'ip': 'IP',
        'ipv4': 'IPv4',
        'ipv6': 'IPv6',
    },
    'MUX_QUIC': {
        '': 'По умолчанию',
        'skip': 'Пропускать',
        'allow': 'Разрешить',
        'reject': 'Блокировать',
    },
}


def get_choice_label(key: str, value: str) -> str:
    """Возвращает human-readable название для choice-значения."""
    labels = CHOICE_LABELS.get(key, {})
    return labels.get(value, value or 'По умолчанию')


_settings: dict[str, Any] = {}

# -- Multi-Provider ID --
# Хранится отдельно от SETTINGS_SCHEMA (сложный список объектов).
# Структура каждого элемента:
#   {
#     "provider_id": "nS5jOH5b",   # Provider ID с happ-proxy.com
#     "squad_uuid": null,           # UUID External Squad в Remnawave (ставится автоматически)
#     "custom_squad": null,         # Привязка к существующему скваду: {"uuid": "...", "name": "..."}
#     "managed": true,              # Управлять ли заголовками этого сквада
#     "overrides": {}               # Переопределения глобальных настроек для этого провайдера
#   }
#
# custom_squad — если задано, модуль управляет заголовками этого сквада
# вместо создания Happ-*. При удалении Provider ID сквад НЕ удаляется.

# Ключи, которые нельзя переопределить на уровне провайдера
NON_OVERRIDABLE_KEYS = frozenset(
    {
        'MODULE_ENABLED',
        'REMNAWAVE_SYNC_ENABLED',
        'HAPP_PROVIDER_ID',
    }
)

PROVIDERS_FILE = 'data/happ_providers.json'
_providers: list[dict] = []
_history: dict[str, int] = {}
_providers_loaded = False


def load_providers() -> list[dict]:
    """Загружает список провайдеров из JSON-файла. Поддерживает старый (list) и новый (dict) формат."""
    global _providers, _history, _providers_loaded
    if os.path.isfile(PROVIDERS_FILE):
        try:
            with open(PROVIDERS_FILE, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                _providers = [p for p in data if isinstance(p, dict)]
                _history = {}
            elif isinstance(data, dict):
                raw_prov = data.get('providers')
                _providers = [p for p in raw_prov if isinstance(p, dict)] if isinstance(raw_prov, list) else []
                raw_hist = data.get('history')
                _history = dict(raw_hist) if isinstance(raw_hist, dict) else {}
            else:
                _providers = []
                _history = {}
            _providers_loaded = True
            return _providers
        except Exception as e:
            logger.warning(f'[HappManagement] Ошибка чтения {PROVIDERS_FILE}: {e}')
    _providers = []
    _history = {}
    _providers_loaded = True
    return _providers


def save_providers() -> None:
    """Сохраняет провайдеров и историю total_assigned в файл."""
    global _providers, _history
    try:
        dirname = os.path.dirname(PROVIDERS_FILE)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(PROVIDERS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'providers': _providers, 'history': _history}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f'[HappManagement] Ошибка сохранения провайдеров: {e}')


def get_providers() -> list[dict]:
    """Возвращает список провайдеров (загружает при необходимости)."""
    if not _providers_loaded:
        load_providers()
    return list(_providers)


def _sync_main_provider() -> None:
    """Автоматически устанавливает HAPP_PROVIDER_ID = первый провайдер из списка."""
    first_pid = _providers[0].get('provider_id', '') if _providers else ''
    current = get('HAPP_PROVIDER_ID')
    if current != first_pid:
        set_value('HAPP_PROVIDER_ID', first_pid)
        logger.info(f'[HappManagement] HAPP_PROVIDER_ID → {first_pid or "(пусто)"}')


def add_provider(provider_id: str) -> bool:
    """Добавляет провайдера. Восстанавливает total_assigned из истории если был ранее."""
    global _providers
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            return False
    restored_count = _history.get(provider_id, 0)
    _providers.append(
        {
            'provider_id': provider_id,
            'squad_uuid': None,
            'custom_squad': None,
            'total_assigned': restored_count,
            'managed': True,
            'overrides': {},
        }
    )
    if restored_count:
        logger.info(
            f'[HappManagement] Провайдер {provider_id} восстановлен из истории: total_assigned={restored_count}'
        )
    save_providers()
    _sync_main_provider()
    return True


def remove_provider(provider_id: str) -> bool:
    """Удаляет провайдера, сохраняя total_assigned в историю."""
    global _providers, _history
    if not _providers_loaded:
        load_providers()
    removed = None
    new_list = []
    for p in _providers:
        if p.get('provider_id') == provider_id:
            removed = p
        else:
            new_list.append(p)
    if removed is None:
        return False
    count = removed.get('total_assigned', 0)
    if count > 0:
        _history[provider_id] = max(_history.get(provider_id, 0), count)
    _providers = new_list
    save_providers()
    _sync_main_provider()
    return True


def update_provider_squad(provider_id: str, squad_uuid: str | None) -> None:
    """Обновляет squad_uuid для провайдера."""
    global _providers
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            p['squad_uuid'] = squad_uuid
            break
    save_providers()


def bind_custom_squad(provider_id: str, squad_uuid: str, squad_name: str) -> bool:
    """Привязывает провайдера к существующему скваду пользователя.
    Возвращает False, если сквад уже привязан к другому провайдеру."""
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            continue
        cs = p.get('custom_squad')
        if isinstance(cs, dict) and cs.get('uuid') == squad_uuid:
            return False
        if p.get('squad_uuid') == squad_uuid:
            return False
    for p in _providers:
        if p.get('provider_id') == provider_id:
            p['custom_squad'] = {'uuid': squad_uuid, 'name': squad_name}
            p['squad_uuid'] = squad_uuid
            save_providers()
            return True
    return False


def unbind_custom_squad(provider_id: str) -> bool:
    """Отвязывает провайдера от пользовательского сквада. squad_uuid сбрасывается."""
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            if not p.get('custom_squad'):
                return False
            p['custom_squad'] = None
            p['squad_uuid'] = None
            save_providers()
            return True
    return False


def get_custom_squad(provider_id: str) -> dict | None:
    """Возвращает привязку к пользовательскому скваду или None."""
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            cs = p.get('custom_squad')
            return cs if isinstance(cs, dict) and cs.get('uuid') else None
    return None


def is_custom_squad(provider_id: str) -> bool:
    """True если провайдер привязан к пользовательскому скваду."""
    return get_custom_squad(provider_id) is not None


def increment_provider_assigned(provider_id: str, *, _defer_save: bool = False) -> int:
    """
    Инкрементирует кумулятивный счётчик total_assigned для провайдера.
    Happ считает устройства навсегда — даже после удаления подписки
    устройство продолжает занимать слот на Provider ID.

    _defer_save: если True, не записывает файл сразу (вызывающий код
    должен вызвать save_providers() после завершения батча).

    Возвращает новое значение счётчика.
    """
    global _providers, _history
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            p['total_assigned'] = p.get('total_assigned', 0) + 1
            _history[provider_id] = max(_history.get(provider_id, 0), p['total_assigned'])
            if not _defer_save:
                save_providers()
            return p['total_assigned']
    return 0


def increment_provider_assigned_to(provider_id: str, value: int) -> None:
    """
    Устанавливает total_assigned = value (синхронизация с membersCount из Remnawave).
    Заменяет старое значение на актуальное из API.
    """
    global _providers, _history
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            current = p.get('total_assigned', 0)
            if value != current:
                p['total_assigned'] = max(0, value)
                _history[provider_id] = max(_history.get(provider_id, 0), value)
                save_providers()
            return


def set_provider_total_assigned(provider_id: str, value: int) -> bool:
    """Устанавливает total_assigned напрямую (ручная корректировка админом)."""
    global _providers, _history
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            p['total_assigned'] = max(0, value)
            _history[provider_id] = p['total_assigned']
            save_providers()
            return True
    return False


def get_provider_total_assigned(provider_id: str) -> int:
    """Возвращает кумулятивный счётчик для провайдера."""
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            return p.get('total_assigned', 0)
    return 0


def get_best_provider_for_assignment() -> tuple[str | None, str | None]:
    """
    Выбирает managed-провайдера с наименьшим total_assigned (< 100) для нового пользователя.
    Возвращает (provider_id, squad_uuid) или (None, None).
    Пропускает провайдеров без squad_uuid (сквад ещё не создан/не синхронизирован).
    """
    if not _providers_loaded:
        load_providers()
    best_pid = None
    best_squad = None
    best_count = 999
    for p in _providers:
        if not p.get('managed', True):
            continue
        squad_uuid = p.get('squad_uuid')
        if not squad_uuid:
            cs = p.get('custom_squad')
            squad_uuid = cs.get('uuid') if isinstance(cs, dict) and cs.get('uuid') else None
        if not squad_uuid:
            continue
        pid = p.get('provider_id', '')
        count = p.get('total_assigned', 0)
        if count < 100 and count < best_count:
            best_count = count
            best_pid = pid
            best_squad = squad_uuid
    return best_pid, best_squad


# -- Per-provider overrides --


def is_provider_managed(provider_id: str) -> bool:
    """Возвращает True если модуль управляет заголовками этого сквада."""
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            return p.get('managed', True)
    return True


def set_provider_managed(provider_id: str, managed: bool) -> None:
    """Устанавливает флаг управления заголовками для провайдера."""
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            p['managed'] = managed
            break
    save_providers()


def get_provider_overrides(provider_id: str) -> dict[str, Any]:
    """Возвращает dict переопределений для провайдера."""
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            return dict(p.get('overrides') or {})
    return {}


def set_provider_override(provider_id: str, key: str, value: Any) -> None:
    """Устанавливает переопределение настройки для провайдера."""
    if key in NON_OVERRIDABLE_KEYS:
        return
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            if 'overrides' not in p or p['overrides'] is None:
                p['overrides'] = {}
            p['overrides'][key] = value
            break
    save_providers()


def remove_provider_override(provider_id: str, key: str) -> None:
    """Удаляет переопределение (провайдер будет использовать глобальное значение)."""
    if not _providers_loaded:
        load_providers()
    for p in _providers:
        if p.get('provider_id') == provider_id:
            overrides = p.get('overrides')
            if overrides and key in overrides:
                del overrides[key]
            break
    save_providers()


def get_effective(key: str, provider_id: str | None = None) -> Any:
    """
    Возвращает эффективное значение настройки с учётом переопределений провайдера.
    Если provider_id не указан или нет переопределения — возвращает глобальное.
    """
    if provider_id:
        overrides = get_provider_overrides(provider_id)
        if key in overrides:
            return overrides[key]
    return get(key)


def is_dependency_met_for_provider(key: str, provider_id: str | None = None) -> bool:
    """Проверяет depends_on с учётом переопределений провайдера."""
    schema = SETTINGS_SCHEMA.get(key)
    if not schema:
        return True
    dep_key = schema.get('depends_on')
    if not dep_key:
        return True
    return bool(get_effective(dep_key, provider_id))


def get_settings_by_categories_for_provider(
    categories: list[str], provider_id: str
) -> list[tuple[str, dict[str, Any], Any, bool]]:
    """
    Возвращает [(ключ, схема, эффективное_значение, is_overridden)] для провайдера.
    Исключает NON_OVERRIDABLE_KEYS.
    """
    cat_set = set(categories)
    overrides = get_provider_overrides(provider_id)
    result = []
    for k, schema in SETTINGS_SCHEMA.items():
        if k in NON_OVERRIDABLE_KEYS:
            continue
        if schema['category'] in cat_set:
            eff = overrides[k] if k in overrides else get(k)
            result.append((k, schema, eff, k in overrides))
    return result


def _defaults() -> dict[str, Any]:
    return {k: v['default'] for k, v in SETTINGS_SCHEMA.items()}


def load_settings() -> dict[str, Any]:
    """Загружает настройки из JSON-файла. Недостающие ключи заполняет значениями по умолчанию."""
    global _settings
    defaults = _defaults()

    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                saved = json.load(f)
            for k in defaults:
                if k not in saved:
                    saved[k] = defaults[k]
            _settings = saved
            logger.info(f'[HappManagement] Настройки загружены из {SETTINGS_FILE}')
            return _settings
        except Exception as e:
            logger.warning(f'[HappManagement] Ошибка чтения {SETTINGS_FILE}: {e}')

    _settings = defaults
    save_settings()
    return _settings


def save_settings() -> None:
    """Сохраняет текущие настройки в JSON-файл."""
    try:
        dirname = os.path.dirname(SETTINGS_FILE)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(_settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f'[HappManagement] Ошибка сохранения: {e}')


def get(key: str) -> Any:
    if not _settings:
        load_settings()
    return _settings.get(key, SETTINGS_SCHEMA.get(key, {}).get('default'))


def set_value(key: str, value: Any) -> None:
    """Обновляет значение и сохраняет в файл."""
    if not _settings:
        load_settings()
    _settings[key] = value
    save_settings()


def validate_value(key: str, value: str) -> str | None:
    """Проверяет значение по правилам из схемы. Возвращает текст ошибки или None."""
    schema = SETTINGS_SCHEMA.get(key)
    if not schema or not value:
        return None

    max_len = schema.get('max_length')
    if max_len and len(value) > max_len:
        return f'Максимум {max_len} символов (сейчас {len(value)})'

    pattern = schema.get('validate')
    if pattern and not re.match(pattern, value):
        return schema.get('validate_hint', 'Невалидное значение')

    val_range = schema.get('validate_range')
    if val_range and value.isdigit():
        lo, hi = val_range
        if not (lo <= int(value) <= hi):
            return f'Допустимый диапазон: {lo}–{hi}'

    return None


# --- Per-host descriptions ---

_HOST_DESCRIPTIONS_KEY = '_host_descriptions'


def get_host_descriptions() -> dict[str, str]:
    """Возвращает {host_uuid: description}."""
    if not _settings:
        load_settings()
    return _settings.get(_HOST_DESCRIPTIONS_KEY, {})


def set_host_description(host_uuid: str, description: str) -> None:
    """Устанавливает описание для конкретного хоста."""
    if not _settings:
        load_settings()
    descs = _settings.setdefault(_HOST_DESCRIPTIONS_KEY, {})
    if description:
        descs[host_uuid] = description[:30]
    else:
        descs.pop(host_uuid, None)
    save_settings()


def clear_host_descriptions() -> None:
    """Удаляет все per-host описания."""
    if not _settings:
        load_settings()
    _settings.pop(_HOST_DESCRIPTIONS_KEY, None)
    save_settings()


def get_settings_by_category(category: str) -> list[tuple[str, dict[str, Any], Any]]:
    """Возвращает [(ключ, схема, текущее_значение)] для категории."""
    result = []
    for k, schema in SETTINGS_SCHEMA.items():
        if schema['category'] == category:
            result.append((k, schema, get(k)))
    return result


def get_settings_by_categories(categories: list[str]) -> list[tuple[str, dict[str, Any], Any]]:
    """Возвращает [(ключ, схема, текущее_значение)] для нескольких категорий (сохраняет порядок schema)."""
    cat_set = set(categories)
    result = []
    for k, schema in SETTINGS_SCHEMA.items():
        if schema['category'] in cat_set:
            result.append((k, schema, get(k)))
    return result


def is_dependency_met(key: str) -> bool:
    """Проверяет, выполнено ли условие depends_on для параметра."""
    schema = SETTINGS_SCHEMA.get(key)
    if not schema:
        return True
    dep_key = schema.get('depends_on')
    if not dep_key:
        return True
    return bool(get(dep_key))


# -- Алерты провайдеров --

PROVIDER_ALERT_THRESHOLDS = [80, 90, 95]
_alerted: set[tuple[str, int]] = set()


def check_provider_alerts() -> list[tuple[str, int, int]]:
    """
    Проверяет пороги заполненности провайдеров.
    Возвращает новые (ещё не отправленные) алерты: [(provider_id, total_assigned, threshold), ...]
    """
    if not _providers_loaded:
        load_providers()
    alerts = []
    for p in _providers:
        pid = p.get('provider_id', '')
        total = p.get('total_assigned', 0)
        for threshold in PROVIDER_ALERT_THRESHOLDS:
            key = (pid, threshold)
            if total >= threshold and key not in _alerted:
                _alerted.add(key)
                alerts.append((pid, total, threshold))
    return alerts


def reset_provider_alerts(provider_id: str | None = None) -> None:
    """Сбрасывает алерты для провайдера (или все, если None)."""
    global _alerted
    if provider_id is None:
        _alerted.clear()
    else:
        _alerted = {(pid, t) for pid, t in _alerted if pid != provider_id}


def is_announce_active(provider_id: str | None = None) -> bool:
    """Проверяет, активно ли объявление с учётом расписания и per-provider overrides."""
    text = get_effective('ANNOUNCE_TEXT', provider_id)
    if not text:
        return False
    start = get_effective('ANNOUNCE_SCHEDULE_START', provider_id)
    end = get_effective('ANNOUNCE_SCHEDULE_END', provider_id)
    if not start or not end:
        return True
    from datetime import datetime

    try:
        now = datetime.now().strftime('%H:%M')
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end
    except Exception:
        return True


_clear_announce_pending: bool = False


def mark_announce_clear() -> None:
    """Помечает, что announce нужно явно очистить в Remnawave при следующей синхронизации."""
    global _clear_announce_pending
    _clear_announce_pending = True


def pop_announce_clear() -> bool:
    """Возвращает True (один раз) если ожидается явная очистка announce."""
    global _clear_announce_pending
    if _clear_announce_pending:
        _clear_announce_pending = False
        return True
    return False


def consume_announce_once() -> None:
    """Если включён режим одноразового объявления — очищает текст и помечает на очистку."""
    if get('ANNOUNCE_ONCE') and get('ANNOUNCE_TEXT'):
        set_value('ANNOUNCE_TEXT', '')
        set_value('ANNOUNCE_ONCE', False)
        mark_announce_clear()
        logger.info('[HappManagement] Одноразовое объявление очищено')


# -- Бэкап / Восстановление --


def export_all() -> dict:
    """Экспортирует все настройки и провайдеров в один dict."""
    if not _settings:
        load_settings()
    if not _providers_loaded:
        load_providers()
    return {
        'version': '2.3.0',
        'settings': dict(_settings),
        'providers': list(_providers),
        'history': dict(_history),
    }


def import_all(data: dict) -> tuple[int, int]:
    """
    Импортирует настройки и провайдеров из dict.
    Возвращает (кол-во настроек, кол-во провайдеров).
    """
    global _settings, _providers, _history
    s_count = 0
    p_count = 0

    if 'settings' in data:
        valid_keys = set(SETTINGS_SCHEMA.keys()) | {_SOURCE_SQUADS_KEY}
        imported = {k: v for k, v in data['settings'].items() if k in valid_keys}
        if not _settings:
            load_settings()
        _settings.update(imported)
        if _SOURCE_SQUADS_KEY in _settings and not isinstance(_settings[_SOURCE_SQUADS_KEY], list):
            _settings[_SOURCE_SQUADS_KEY] = []
        save_settings()
        s_count = len(imported)

    if 'providers' in data and isinstance(data['providers'], list):
        validated = []
        for p in data['providers']:
            if isinstance(p, dict) and p.get('provider_id'):
                raw_overrides = p.get('overrides')
                overrides = {str(k): v for k, v in raw_overrides.items()} if isinstance(raw_overrides, dict) else {}
                item = {
                    'provider_id': str(p['provider_id']),
                    'squad_uuid': p.get('squad_uuid'),
                    'total_assigned': int(p.get('total_assigned', 0)),
                    'managed': bool(p.get('managed', True)),
                    'overrides': overrides,
                }
                raw_cs = p.get('custom_squad')
                if isinstance(raw_cs, dict) and raw_cs.get('uuid'):
                    item['custom_squad'] = {'uuid': str(raw_cs['uuid']), 'name': str(raw_cs.get('name', ''))}
                validated.append(item)
        _providers = validated
        raw_history = data.get('history', {})
        _history = {str(k): int(v) for k, v in raw_history.items()} if isinstance(raw_history, dict) else {}
        save_providers()
        _sync_main_provider()
        p_count = len(_providers)

    return s_count, p_count


# -- Source Squads (источники пользователей) --
# Хранится в settings JSON под ключом _SOURCE_SQUADS.
# Список сквадов, из которых модуль может забирать пользователей.
# Если список пуст и REASSIGN_FROM_FOREIGN_SQUADS=True -> забирает из ВСЕХ.
# Если список не пуст -> забирает только из указанных.
# Структура: [{"uuid": "...", "name": "..."}]

_SOURCE_SQUADS_KEY = '_SOURCE_SQUADS'


def _ensure_source_squads_list() -> list[dict]:
    """Возвращает валидный список сквадов-источников, нормализуя при необходимости."""
    if not _settings:
        load_settings()
    raw = _settings.get(_SOURCE_SQUADS_KEY)
    if not isinstance(raw, list):
        _settings[_SOURCE_SQUADS_KEY] = []
        return []
    valid = [s for s in raw if isinstance(s, dict) and s.get('uuid')]
    if len(valid) != len(raw):
        _settings[_SOURCE_SQUADS_KEY] = valid
    return valid


def get_source_squads() -> list[dict]:
    """Возвращает список сквадов-источников [{uuid, name}]."""
    return list(_ensure_source_squads_list())


def get_source_squad_uuids() -> set[str]:
    """Возвращает set UUID сквадов-источников для быстрой проверки."""
    return {s['uuid'] for s in _ensure_source_squads_list()}


def add_source_squad(uuid: str, name: str) -> bool:
    """Добавляет сквад в список источников. Возвращает False если уже есть."""
    squads = _ensure_source_squads_list()
    for s in squads:
        if s.get('uuid') == uuid:
            return False
    squads.append({'uuid': uuid, 'name': name})
    _settings[_SOURCE_SQUADS_KEY] = squads
    save_settings()
    return True


def remove_source_squad(uuid: str) -> bool:
    """Удаляет сквад из списка источников. Возвращает False если не найден."""
    squads = _ensure_source_squads_list()
    new_list = [s for s in squads if s.get('uuid') != uuid]
    if len(new_list) == len(squads):
        return False
    _settings[_SOURCE_SQUADS_KEY] = new_list
    save_settings()
    return True


def clear_source_squads() -> int:
    """Очищает весь список источников. Возвращает количество удалённых."""
    squads = _ensure_source_squads_list()
    count = len(squads)
    _settings[_SOURCE_SQUADS_KEY] = []
    save_settings()
    return count


def is_source_squad(uuid: str) -> bool:
    """Проверяет, является ли сквад источником."""
    return uuid in get_source_squad_uuids()


# -- Happ Accounts (авторегистрация) --

ACCOUNTS_FILE = 'data/happ_accounts.json'
_accounts: list[dict] = []
_accounts_loaded = False


def load_accounts() -> list[dict]:
    """Загружает список зарегистрированных аккаунтов happ-proxy.com."""
    global _accounts, _accounts_loaded
    if os.path.isfile(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, encoding='utf-8') as f:
                data = json.load(f)
            _accounts = data.get('accounts', []) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning(f'[HappManagement] Ошибка загрузки аккаунтов: {e}')
            _accounts = []
    else:
        _accounts = []
    _accounts_loaded = True
    return _accounts


def save_accounts() -> None:
    """Сохраняет список аккаунтов в файл."""
    try:
        dirname = os.path.dirname(ACCOUNTS_FILE)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'accounts': _accounts}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f'[HappManagement] Ошибка сохранения аккаунтов: {e}')


def get_accounts() -> list[dict]:
    """Возвращает список аккаунтов (загружает при необходимости)."""
    if not _accounts_loaded:
        load_accounts()
    return list(_accounts)


def add_account(account: dict) -> None:
    """Добавляет аккаунт в хранилище."""
    global _accounts
    if not _accounts_loaded:
        load_accounts()
    _accounts.append(account)
    save_accounts()


def remove_account(provider_id: str) -> bool:
    """Удаляет аккаунт по provider_id."""
    global _accounts
    if not _accounts_loaded:
        load_accounts()
    new_list = [a for a in _accounts if a.get('provider_id') != provider_id]
    if len(new_list) == len(_accounts):
        return False
    _accounts = new_list
    save_accounts()
    return True
