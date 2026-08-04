"""Отключённую подписку можно продлить — это способ вернуть доступ.

Прод: кабинет отвечал `Cannot renew subscription with status: disabled`.
Статус DISABLED проставляется автоматически (истечение, отключение в панели,
fallback), поэтому запрет на продление означал, что человек не может заплатить
и вернуть себе VPN. При этом `extend_subscription` такую подписку обрабатывает
как истёкшую и переводит обратно в active — мешал только гейт в роуте.
"""

from __future__ import annotations

from app.cabinet.routes.subscription_modules import renewal
from app.database.models import SubscriptionStatus


def _non_renewable_statuses() -> set[str]:
    """Достаём множество из исходника роута — оно объявлено локально в двух местах."""
    import inspect
    import re

    src = inspect.getsource(renewal)
    matches = re.findall(r'_non_renewable = \{([^}]+)\}', src)
    assert matches, 'не найдено объявление _non_renewable'
    statuses = set()
    for block in matches:
        for name in re.findall(r'SubscriptionStatus\.(\w+)\.value', block):
            statuses.add(getattr(SubscriptionStatus, name).value)
    return statuses


def test_disabled_subscription_is_renewable():
    assert SubscriptionStatus.DISABLED.value not in _non_renewable_statuses()


def test_pending_subscription_is_still_not_renewable():
    """PENDING остаётся запрещённым: подписки в панели ещё нет, продлевать нечего."""
    assert SubscriptionStatus.PENDING.value in _non_renewable_statuses()


def test_expired_and_limited_are_renewable():
    non_renewable = _non_renewable_statuses()
    assert SubscriptionStatus.EXPIRED.value not in non_renewable
    assert SubscriptionStatus.LIMITED.value not in non_renewable
