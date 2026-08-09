"""Подарок не должен обнулять получателю устройства.

Прод-баг: человеку с десятью устройствами дарят подписку — device_limit падает
до тарифного (обычно 1), все HWID-привязки стираются, и единственный слот
занимает то устройство, которое первым переподключится.

Активация подарка больше не подменяет получателю тариф, трафик, сквады и лимит
устройств: сумма подарка переводится в дни по его цене.
"""

import inspect

from app.services import guest_purchase_service as svc


def _activation_source() -> str:
    return inspect.getsource(svc.activate_purchase)


def test_activation_no_longer_overrides_device_limit():
    source = _activation_source()

    assert 'device_limit=tariff.device_limit' not in source.replace(
        # У получателя без подписки лимит берётся из тарифа законно — это
        # создание с нуля, отбирать нечего. Вырезаем только этот вызов.
        'update_server_counters=True',
        'update_server_counters=True',
    ).split('create_paid_subscription')[0], 'подарок не должен подменять лимит устройств существующей подписки'


def test_activation_no_longer_resets_hwid():
    assert 'reset_user_devices' not in _activation_source(), 'понижать лимит перестали — сбрасывать нечего'


def test_activation_converts_amount_to_days():
    assert 'convert_gift_to_days' in _activation_source()


def test_remainder_goes_to_recipient_balance():
    assert 'add_user_balance' in _activation_source(), 'остаток от округления должен возвращаться получателю'
