from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.handlers.subscription.purchase import activate_trial
from app.services.trial_activation_service import TrialPaymentInsufficientFunds


@pytest.fixture
def trial_callback_query():
    callback = AsyncMock(spec=CallbackQuery)
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.fixture
def trial_user():
    user = MagicMock(spec=User)
    user.subscription = None
    user.has_had_paid_subscription = False
    user.language = 'ru'
    user.restriction_subscription = False
    user.auth_type = 'telegram'
    return user


@pytest.fixture
def trial_db():
    db = AsyncMock(spec=AsyncSession)
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_activate_trial_uses_trial_price_for_topup_redirect(
    trial_callback_query,
    trial_user,
    trial_db,
):
    error = TrialPaymentInsufficientFunds(required_amount=15900, balance_amount=100)

    mock_keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    mock_subscription = MagicMock()

    with (
        patch(
            'app.handlers.subscription.purchase.settings',
        ) as mock_settings,
        patch(
            'app.handlers.subscription.purchase.get_texts',
            return_value=MagicMock(
                t=lambda key, default, **kwargs: default,
                BACK='Назад',
                TRIAL_ALREADY_USED='Триал уже использован',
            ),
        ),
        patch(
            'app.handlers.subscription.purchase.create_trial_subscription',
            new_callable=AsyncMock,
            return_value=mock_subscription,
        ),
        patch(
            'app.handlers.subscription.purchase.charge_trial_activation_if_required',
            new_callable=AsyncMock,
            side_effect=error,
        ),
        patch(
            'app.handlers.subscription.purchase.rollback_trial_subscription_activation',
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            'app.handlers.subscription.purchase.get_insufficient_balance_keyboard',
            return_value=mock_keyboard,
        ) as insufficient_keyboard,
    ):
        mock_settings.is_trial_disabled_for_user.return_value = False
        mock_settings.is_tariffs_mode.return_value = False
        mock_settings.is_devices_selection_enabled.return_value = False
        mock_settings.get_disabled_mode_device_limit.return_value = None
        mock_settings.TRIAL_TRAFFIC_LIMIT_GB = 10
        mock_settings.TRIAL_DURATION_DAYS = 7
        mock_settings.TRIAL_DEVICE_LIMIT = 1
        mock_settings.format_price = lambda x: f'{x / 100:.0f} ₽'
        mock_settings.get_support_contact_url.return_value = None

        # get_trial_activation_charge_amount — lazy import внутри activate_trial,
        # мокируем в исходном модуле
        with patch(
            'app.services.trial_activation_service.get_trial_activation_charge_amount',
            return_value=0,
        ):
            await activate_trial(trial_callback_query, trial_user, trial_db)

    insufficient_keyboard.assert_called_once_with(
        trial_user.language,
        amount_kopeks=error.required_amount,
    )
    trial_callback_query.message.edit_text.assert_called_once()
    trial_callback_query.answer.assert_called_once()
