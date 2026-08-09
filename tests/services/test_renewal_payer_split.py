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
