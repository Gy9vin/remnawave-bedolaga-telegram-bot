"""Рассылка промопредложений не должна брать пул соединений штурмом.

Прод-траса: `send_single_offer` упал `TimeoutError` на открытии соединения,
застряв в `getaddrinfo`. Каждая отправка открывает СВОЮ сессию, а параллельных
отправок было двадцать — то есть двадцать одновременных подключений к базе, и
каждое сперва резолвит имя хоста через встроенный DNS докера. Под таким залпом
UDP-запросы теряются, и потерянный резолв висит все десять секунд таймаута.

Двадцать здесь и не нужны: узкое место рассылки — лимиты Telegram, а не база.
"""

import inspect

from app.config import settings
from app.handlers.admin import promo_offers


def test_concurrency_is_configurable():
    assert hasattr(settings, 'PROMO_BROADCAST_CONCURRENCY')


def test_default_concurrency_is_modest():
    """Двадцать одновременных соединений к базе ради отправки сообщений — перебор."""
    assert 1 <= settings.PROMO_BROADCAST_CONCURRENCY <= 10


def test_broadcast_uses_the_setting_not_a_literal():
    source = inspect.getsource(promo_offers._send_offer_to_users)

    assert 'PROMO_BROADCAST_CONCURRENCY' in source
    assert 'Semaphore(20)' not in source
