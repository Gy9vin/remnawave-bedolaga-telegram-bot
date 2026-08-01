"""T2 — activate_trial при нехватке баланса сохраняет корзину trial_purchase и возвращает 402.

Проверяем, что когда у пользователя нет средств на платный триал:
- вызывается user_cart_service.save_user_cart с cart_mode='trial_purchase',
  return_to_cart=True, total_price=TRIAL_ACTIVATION_PRICE;
- выбрасывается HTTPException со status_code=402, detail['code']=='insufficient_funds',
  detail['cart_mode']=='trial_purchase', detail['cart_saved']==True.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config import settings


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------


def _make_user(balance_kopeks: int = 0) -> MagicMock:
    """Пользователь без активных подписок, триал ещё не использован."""
    user = MagicMock()
    user.id = 42
    user.balance_kopeks = balance_kopeks
    user.auth_type = 'telegram'
    # Нет активных подписок
    user.subscriptions = []
    # is_trial_already_used() → False
    user.is_trial_already_used = MagicMock(return_value=False)
    return user


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# Тест
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_trial_insufficient_balance_saves_cart_and_raises_402(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При нехватке баланса на платный триал сохраняется корзина trial_purchase и выбрасывается 402."""
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 1000)
    # is_trial_disabled_for_user должен вернуть False (триал доступен)
    monkeypatch.setattr(type(settings), 'is_trial_disabled_for_user', lambda self, auth_type: False)

    user = _make_user(balance_kopeks=0)
    db = _make_db()

    mock_save_cart = AsyncMock()

    from app.cabinet.routes.subscription_modules import purchase as purchase_module

    with patch.object(purchase_module.user_cart_service, 'save_user_cart', mock_save_cart):
        with pytest.raises(HTTPException) as exc_info:
            await purchase_module.activate_trial(
                request=None,
                user=user,
                db=db,
            )

    # Проверяем HTTP 402
    exc = exc_info.value
    assert exc.status_code == 402, f'Ожидался 402, получен {exc.status_code}'

    # Проверяем detail
    detail = exc.detail
    assert isinstance(detail, dict), f'detail должен быть dict, получен {type(detail)}'
    assert detail.get('code') == 'insufficient_funds', f"detail['code'] = {detail.get('code')!r}"
    assert detail.get('cart_mode') == 'trial_purchase', f"detail['cart_mode'] = {detail.get('cart_mode')!r}"
    assert detail.get('cart_saved') is True, f"detail['cart_saved'] = {detail.get('cart_saved')!r}"

    # Проверяем, что save_user_cart был вызван с корректными аргументами
    mock_save_cart.assert_called_once()
    call_args = mock_save_cart.call_args
    # Первый позиционный аргумент — user_id
    assert call_args.args[0] == user.id, f'user_id в вызове save_user_cart: {call_args.args[0]}'
    # Второй аргумент — cart_data
    cart_data = call_args.args[1]
    assert cart_data['cart_mode'] == 'trial_purchase', f"cart_data['cart_mode'] = {cart_data['cart_mode']!r}"
    assert cart_data['return_to_cart'] is True, f"cart_data['return_to_cart'] = {cart_data['return_to_cart']!r}"
    assert cart_data['total_price'] == 1000, f"cart_data['total_price'] = {cart_data['total_price']!r}"
