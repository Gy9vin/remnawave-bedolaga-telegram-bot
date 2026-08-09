"""Схемы для оплаты подписки за другого человека из кабинета.

Ответы намеренно узкие: получатель не давал согласия на то, чтобы плательщик
видел его тариф, дату окончания, число устройств, telegram id или email — это
чужие данные. Наружу уходит только то, что нужно, чтобы выбрать период и
подтвердить оплату: отображаемое имя, id подписки (для последующего pay) и
цены по периодам.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SponsoredLookupRequest(BaseModel):
    """То, что вводит плательщик — @ник, telegram id или email получателя."""

    query: str = Field(min_length=1, max_length=255)


class SponsoredPeriodOption(BaseModel):
    period_days: int
    price_kopeks: int


class SponsoredLookupResponse(BaseModel):
    """Публичная витрина получателя — без тарифа, срока, устройств и контактов."""

    recipient_display_name: str
    subscription_id: int | None
    options: list[SponsoredPeriodOption]
    payer_balance_kopeks: int


class SponsoredPayRequest(BaseModel):
    """Получателя резолвим заново по ``query`` — id с клиента не доверяем."""

    query: str = Field(min_length=1, max_length=255)
    period_days: int = Field(gt=0, le=3650)


class SponsoredPayResponse(BaseModel):
    status: str
    recipient_display_name: str
    period_days: int
    amount_kopeks: int
