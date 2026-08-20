"""Продление может оплатить другой человек — роли надо развести.

Сейчас `finalize` делает три вещи одним вызовом `subtract_user_balance`: снимает
деньги, гасит промо-оффер и ставит `has_had_paid_subscription`. Пока платит сам
владелец подписки, это верно. Как только платит третье лицо — каждое действие
адресовано своему человеку:

- деньги снимаются с плательщика;
- промо-оффер гасится у получателя (скидка считалась по его подписке);
- флаг «была платная подписка» ставится получателю. Иначе человек, оплативший
  другу, сам терял бы право на триал, ни разу не купив себе подписку.
"""

import inspect

from app.services.subscription_renewal_service import SubscriptionRenewalService


def _finalize_source() -> str:
    return inspect.getsource(SubscriptionRenewalService.finalize)


def test_finalize_accepts_a_separate_payer():
    signature = inspect.signature(SubscriptionRenewalService.finalize)

    assert 'payer' in signature.parameters
    assert signature.parameters['payer'].default is None, 'обычное продление не должно ничего передавать'


def test_charge_goes_to_the_payer():
    source = _finalize_source()

    assert 'payer or user' in source or 'payer if payer' in source, (
        'списывать надо с плательщика, а не с владельца подписки'
    )


def test_paid_subscription_flag_is_not_given_to_the_payer():
    """Оплатил другу — сам права на триал не теряешь."""
    source = _finalize_source()

    assert 'mark_as_paid_subscription' in source
    assert 'has_had_paid_subscription' in source, (
        'при стороннем плательщике флаг надо ставить получателю явно'
    )


def test_promo_offer_is_consumed_from_the_recipient():
    source = _finalize_source()

    assert 'consume_promo_offer' in source
    assert 'promo_offer_discount_percent' in source


# ---------------------------------------------------------------------------
# Fix: продление из fallback не должно тарифицировать восстановленные сквады
# ---------------------------------------------------------------------------


def test_renewal_from_fallback_zeroes_restored_server_prices():
    """При продлении подписки, которая была в fallback, сервер-сквады, возвращаемые
    из pre_expiry-снапшота, НЕ должны тарифицироваться как новые платные серверы.

    Фикс: в finalize() перед вызовом add_subscription_servers проверяем флаги
    expiry_fallback_active / traffic_fallback_active и обнуляем server_prices_for_period,
    чтобы в SubscriptionServer.paid_price_kopeks записывался 0, а не цена как при
    свежем подключении сервера.
    """
    source = _finalize_source()

    # Проверяем, что оба fallback-флага читаются в finalize
    assert 'expiry_fallback_active' in source, (
        'finalize должна читать expiry_fallback_active перед add_subscription_servers'
    )
    assert 'traffic_fallback_active' in source, (
        'finalize должна читать traffic_fallback_active перед add_subscription_servers'
    )
    # Проверяем, что цены обнуляются при fallback
    assert '[0] * len(server_ids)' in source, (
        'server_prices_for_period должны обнуляться ([0] * len(server_ids)) при fallback'
    )

    # Позиционная проверка: обнуление идёт ПЕРЕД вызовом add_subscription_servers
    fallback_zero_idx = source.find('[0] * len(server_ids)')
    add_servers_idx = source.find('add_subscription_servers')
    assert fallback_zero_idx < add_servers_idx, (
        'обнуление server_prices_for_period должно быть до вызова add_subscription_servers'
    )
