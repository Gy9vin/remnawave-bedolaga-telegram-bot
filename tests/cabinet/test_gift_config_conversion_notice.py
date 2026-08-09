"""Даритель должен заранее знать, что подарок зачтётся по цене получателя.

Подарок по коду больше не выдаёт фиксированный пакет: его сумма переводится в
дни по тарифу того, кто активирует. Человек, купивший «месяц», может увидеть у
получателя 13 дней — и без предупреждения пойдёт в поддержку с претензией.
"""

from app.cabinet.schemas.gift import GiftConfigResponse


def test_config_carries_the_conversion_notice():
    response = GiftConfigResponse(is_enabled=True)

    assert response.value_conversion_notice
    assert 'тариф' in response.value_conversion_notice.lower()


def test_notice_mentions_both_directions_and_the_remainder():
    notice = GiftConfigResponse(is_enabled=True).value_conversion_notice.lower()

    assert 'меньше' in notice, 'дороже у получателя — дней меньше'
    assert 'больше' in notice, 'дешевле — дней больше'
    assert 'баланс' in notice, 'остаток зачисляется на баланс'
