"""Оплата подписки за другого человека — цена по получателю, история платежа.

Сценарий: плательщик находит получателя (``resolve_recipient``, отдельный
модуль) и оплачивает ему продление. Три требования, которые легко сломать
незаметно:

- цена считается по подписке ПОЛУЧАТЕЛЯ (его скидки, его допустройства), а не
  плательщика — иначе более щедрая скидка плательщика подставится не туда;
- нехватка баланса плательщика не должна оставлять «мусорную» запись платежа —
  запись это история совершённого платежа, а не заявка на него;
- применение платежа (``apply_payment``) идемпотентно: вебхук платёжки может
  прийти повторно, повторное продление недопустимо, а сбой обязан перевести
  запись в ``failed``, а не оставить её в подвешенном состоянии.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.database.models import SponsoredPaymentStatus
from app.services import sponsored_payment_service as svc


def _user(user_id: int, *, balance_kopeks: int = 0, full_name: str = 'Получатель') -> SimpleNamespace:
    return SimpleNamespace(id=user_id, balance_kopeks=balance_kopeks, full_name=full_name)


def _tariff(period_prices: dict | None, is_active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(is_active=is_active, period_prices=period_prices)


def _subscription(sub_id: int = 1, tariff_id: int | None = None, tariff=None) -> SimpleNamespace:
    return SimpleNamespace(id=sub_id, tariff_id=tariff_id, tariff=tariff)


def _pricing(final_total: int, original_total: int | None = None) -> SimpleNamespace:
    """Заглушка RenewalPricing — важны только поля, которые читает наш код."""
    total = final_total if original_total is None else original_total
    return SimpleNamespace(final_total=final_total, original_total=total)


class _FakeResult:
    def __init__(self, item):
        self._item = item

    def scalars(self):
        inner = self

        class _Scalars:
            def first(_self):
                return inner._item

        return _Scalars()


def _fake_db(execute_result=None):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result or _FakeResult(None))
    # db.add — синхронный метод SQLAlchemy-сессии, AsyncMock по умолчанию
    # сделал бы его корутиной и тест ругался бы на неawait-нутый вызов.
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# quote_for_recipient — цена по получателю
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quote_uses_recipient_pricing_not_payer(monkeypatch):
    """Ключевая проверка: calculate_renewal_price должен получить user=recipient."""
    recipient = _user(2, full_name='Иван Получателев')
    subscription = _subscription(tariff_id=None, tariff=None)

    monkeypatch.setattr(svc, 'get_subscription_by_user_id', AsyncMock(return_value=subscription))
    monkeypatch.setattr(Settings, 'get_available_renewal_periods', lambda self: [30, 90])

    calculate = AsyncMock(side_effect=[_pricing(10000), _pricing(25000)])
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', calculate)

    quote = await svc.quote_for_recipient(_fake_db(), recipient)

    assert calculate.await_count == 2
    for call in calculate.await_args_list:
        assert call.kwargs.get('user') is recipient

    assert quote.recipient_id == 2
    assert quote.recipient_display_name == 'Иван Получателев'
    assert quote.subscription_id == subscription.id
    assert quote.options == [(30, 10000), (90, 25000)]


@pytest.mark.asyncio
async def test_quote_display_name_has_no_tariff_or_period_info():
    """recipient_display_name — только имя, без тарифа/срока/устройств."""
    recipient = _user(3, full_name='Мария')
    subscription = None  # у получателя ещё нет подписки

    monkeypatch_marker = None  # используем прямой вызов без monkeypatch для наглядности
    del monkeypatch_marker

    # Подмена достаточно локальна, чтобы не тянуть monkeypatch fixture лишний раз.
    original = svc.get_subscription_by_user_id
    svc.get_subscription_by_user_id = AsyncMock(return_value=subscription)
    try:
        quote = await svc.quote_for_recipient(_fake_db(), recipient)
    finally:
        svc.get_subscription_by_user_id = original

    assert quote.recipient_display_name == 'Мария'
    assert quote.subscription_id is None
    assert quote.options == []


@pytest.mark.asyncio
async def test_quote_uses_tariff_period_prices_when_available(monkeypatch):
    """Периоды берутся из тарифа получателя, если он активен и задаёт period_prices."""
    tariff = _tariff({'60': 1, '120': 1})
    subscription = _subscription(tariff_id=5, tariff=tariff)
    recipient = _user(4)

    monkeypatch.setattr(svc, 'get_subscription_by_user_id', AsyncMock(return_value=subscription))
    calculate = AsyncMock(return_value=_pricing(5000))
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', calculate)
    get_periods = MagicMock(return_value=[30])
    monkeypatch.setattr(Settings, 'get_available_renewal_periods', lambda self: get_periods())

    quote = await svc.quote_for_recipient(_fake_db(), recipient)

    get_periods.assert_not_called()
    assert [period for period, _ in quote.options] == [60, 120]


# ---------------------------------------------------------------------------
# pay_from_balance — нехватка баланса, payer в finalize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_balance_raises_before_creating_a_record(monkeypatch):
    payer = _user(1, balance_kopeks=100)
    recipient = _user(2)
    subscription = _subscription()

    finalize = AsyncMock()
    monkeypatch.setattr(svc.SubscriptionRenewalService, 'finalize', finalize)
    db = _fake_db()

    with pytest.raises(svc.InsufficientBalanceError):
        await svc.pay_from_balance(
            db,
            payer=payer,
            recipient=recipient,
            subscription=subscription,
            period_days=30,
            price_kopeks=10000,
        )

    finalize.assert_not_awaited()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_pay_from_balance_passes_payer_to_finalize(monkeypatch):
    payer = _user(1, balance_kopeks=100000)
    recipient = _user(2, full_name='Пётр')
    subscription = _subscription(sub_id=42, tariff_id=7)

    finalize = AsyncMock()
    monkeypatch.setattr(svc.SubscriptionRenewalService, 'finalize', finalize)
    db = _fake_db()

    payment = await svc.pay_from_balance(
        db,
        payer=payer,
        recipient=recipient,
        subscription=subscription,
        period_days=30,
        price_kopeks=10000,
    )

    finalize.assert_awaited_once()
    _, call_args, call_kwargs = finalize.mock_calls[0]
    # finalize(self, db, user, subscription, pricing, *, ..., payer=...)
    assert call_args[1] is recipient
    assert call_args[2] is subscription
    assert call_kwargs['payer'] is payer

    assert payment.status == SponsoredPaymentStatus.APPLIED.value
    assert payment.amount_kopeks == 10000
    assert payment.applied_at is not None
    db.add.assert_called_once_with(payment)
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_pay_from_balance_does_not_recalculate_the_fixed_price(monkeypatch):
    """Цена уже посчитана в quote_for_recipient — здесь её не пересчитываем."""
    payer = _user(1, balance_kopeks=100000)
    recipient = _user(2)
    subscription = _subscription()

    monkeypatch.setattr(svc.SubscriptionRenewalService, 'finalize', AsyncMock())
    calculate = AsyncMock()
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', calculate)
    db = _fake_db()

    await svc.pay_from_balance(
        db,
        payer=payer,
        recipient=recipient,
        subscription=subscription,
        period_days=30,
        price_kopeks=12345,
    )

    calculate.assert_not_awaited()


# ---------------------------------------------------------------------------
# apply_payment — идемпотентность и обработка сбоя
# ---------------------------------------------------------------------------


def _sponsored_payment(**overrides):
    from app.database.models import SponsoredPayment

    base = dict(
        id=1,
        payer_user_id=1,
        recipient_user_id=2,
        subscription_id=42,
        tariff_id=None,
        period_days=30,
        amount_kopeks=10000,
        status=SponsoredPaymentStatus.PENDING.value,
        payment_method='yookassa',
        payment_id='ext-123',
        applied_at=None,
    )
    base.update(overrides)
    payment = SponsoredPayment(**{k: v for k, v in base.items() if k not in ('id',)})
    payment.id = base['id']
    payment.payer = _user(base['payer_user_id'])
    payment.recipient = _user(base['recipient_user_id'])
    payment.subscription = _subscription(sub_id=base['subscription_id'])
    return payment


@pytest.mark.asyncio
async def test_apply_payment_is_idempotent(monkeypatch):
    """Повторный apply_payment на уже применённой записи не продлевает подписку снова."""
    payment = _sponsored_payment(status=SponsoredPaymentStatus.APPLIED.value)
    db = _fake_db(_FakeResult(payment))

    finalize = AsyncMock()
    monkeypatch.setattr(svc.SubscriptionRenewalService, 'finalize', finalize)

    result = await svc.apply_payment(db, 'ext-123')

    assert result is True
    finalize.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_payment_finalizes_with_fixed_amount_and_zero_balance_charge(monkeypatch):
    """Деньги уже получены внешней платёжкой — с баланса получателя списывать 0."""
    payment = _sponsored_payment()
    db = _fake_db(_FakeResult(payment))

    finalize = AsyncMock()
    monkeypatch.setattr(svc.SubscriptionRenewalService, 'finalize', finalize)

    result = await svc.apply_payment(db, 'ext-123')

    assert result is True
    finalize.assert_awaited_once()
    _, call_args, call_kwargs = finalize.mock_calls[0]
    assert call_args[1] is payment.recipient
    assert call_args[2] is payment.subscription
    assert call_kwargs['payer'] is payment.payer
    assert call_kwargs['charge_balance_amount'] == 0

    pricing_arg = call_args[3]
    assert pricing_arg.final_total == payment.amount_kopeks

    assert payment.status == SponsoredPaymentStatus.APPLIED.value
    assert payment.applied_at is not None
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_apply_payment_failure_marks_the_record_failed(monkeypatch):
    payment = _sponsored_payment()
    db = _fake_db(_FakeResult(payment))

    finalize = AsyncMock(side_effect=RuntimeError('панель недоступна'))
    monkeypatch.setattr(svc.SubscriptionRenewalService, 'finalize', finalize)

    result = await svc.apply_payment(db, 'ext-123')

    assert result is False
    assert payment.status == SponsoredPaymentStatus.FAILED.value
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_apply_payment_returns_false_when_not_found():
    db = _fake_db(_FakeResult(None))

    result = await svc.apply_payment(db, 'unknown')

    assert result is False
