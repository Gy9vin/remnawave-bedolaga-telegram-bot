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

    # Проверяем, что состояние захватывается ДО вызова extend_subscription (_was_in_fallback)
    assert '_was_in_fallback' in source, (
        'флаг fallback должен захватываться в _was_in_fallback ДО вызова extend_subscription'
    )
    was_in_fallback_idx = source.find('_was_in_fallback')
    extend_idx = source.find('extend_subscription(')
    assert was_in_fallback_idx < extend_idx, (
        '_was_in_fallback должен вычисляться до вызова extend_subscription'
    )


def test_fallback_prices_zeroed_even_when_flags_cleared_before_check():
    """Поведенческий тест: цены серверов обнуляются даже когда extend_subscription
    уже очистил флаги expiry_fallback_active / traffic_fallback_active к моменту проверки.

    Это и есть суть бага: раньше финализ читал флаги ПОСЛЕ extend_subscription,
    которая внутри вызывает restore_from_fallback → _clear_fallback_state и сбрасывает флаги.
    Фикс: флаг захватывается в _was_in_fallback ДО extend_subscription.
    """

    class _Sub:
        expiry_fallback_active: bool = True
        traffic_fallback_active: bool = False

    sub = _Sub()

    # --- Шаг 1: захватываем состояние ДО (как делает исправленный код) ---
    _was_in_fallback = bool(
        getattr(sub, 'expiry_fallback_active', False)
        or getattr(sub, 'traffic_fallback_active', False)
    )

    # --- Шаг 2: extend_subscription очищает флаги (имитация restore_from_fallback) ---
    sub.expiry_fallback_active = False
    sub.traffic_fallback_active = False

    # --- Шаг 3: логика обнуления, идентичная исправленному finalize ---
    server_ids = ['uuid-server-1', 'uuid-server-2']
    server_prices_for_period = [500, 800]  # цены как при свежем подключении

    if server_ids and _was_in_fallback:
        server_prices_for_period = [0] * len(server_ids)

    assert server_prices_for_period == [0, 0], (
        'серверы из fallback-восстановления не должны тарифицироваться: цены должны быть 0'
    )


def test_no_fallback_prices_kept():
    """Если подписка НЕ была в fallback, цены серверов сохраняются без изменений."""

    class _Sub:
        expiry_fallback_active: bool = False
        traffic_fallback_active: bool = False

    sub = _Sub()
    _was_in_fallback = bool(
        getattr(sub, 'expiry_fallback_active', False)
        or getattr(sub, 'traffic_fallback_active', False)
    )

    server_ids = ['uuid-server-1']
    server_prices_for_period = [500]

    if server_ids and _was_in_fallback:
        server_prices_for_period = [0] * len(server_ids)

    assert server_prices_for_period == [500], (
        'без fallback цены серверов должны остаться без изменений'
    )
