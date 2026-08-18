"""Название клиентского приложения в списке устройств.

hwid выдаётся на установку приложения, а не на физическое устройство: Happ и
INCY на одном телефоне занимают два места. Без названия клиента человек видит
список, где устройств больше, чем у него есть, и идёт в поддержку.

Панель хранит агент прямо в записи устройства (HwidUserDevices.userAgent),
поэтому дополнительных запросов не нужно — поле просто не читалось.
"""

import pytest

from app.cabinet.routes.subscription_modules.devices import extract_client_name


@pytest.mark.parametrize(
    'user_agent, expected',
    [
        ('Happ/2.1.0 (iPhone; iOS 17.4)', 'Happ'),
        ('Streisand/1.2.3 (iPad; iPadOS 17)', 'Streisand'),
        ('INCY/3.0', 'INCY'),
        ('Hiddify', 'Hiddify'),
        ('  Happ/2.1  ', 'Happ'),
    ],
)
def test_client_name_is_trimmed_of_version_and_platform(user_agent, expected):
    assert extract_client_name(user_agent) == expected


@pytest.mark.parametrize('empty', ['', '   ', None])
def test_missing_agent_gives_none(empty):
    """Пустое поле честнее выдуманного «Unknown» — фронт покажет платформу."""
    assert extract_client_name(empty) is None


@pytest.mark.parametrize('garbage', [42, [], {}, True])
def test_non_string_agent_is_safe(garbage):
    assert extract_client_name(garbage) is None


def test_agent_without_name_part_gives_none():
    """Агент из одних разделителей не даёт имени."""
    assert extract_client_name('/1.0') is None
