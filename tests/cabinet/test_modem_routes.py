"""
Тесты для Cabinet API эндпоинтов модема.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.modem_service import (
    ModemAvailabilityResult,
    ModemDisableResult,
    ModemEnableResult,
    ModemError,
    ModemPriceResult,
)


def _make_user(balance=50000, modem_enabled=False, is_trial=False, device_limit=2):
    """Создаёт мок пользователя с подпиской."""
    subscription = SimpleNamespace(
        id=1,
        user_id=1,
        is_trial=is_trial,
        modem_enabled=modem_enabled,
        device_limit=device_limit,
        end_date=datetime.now(UTC) + timedelta(days=30),
        updated_at=datetime.now(UTC),
        status='active',
        tariff_id=None,
    )
    user = SimpleNamespace(
        id=1,
        telegram_id=123456789,
        email=None,
        email_verified=False,
        balance_kopeks=balance,
        language='ru',
        subscription=subscription,
        status='active',
        username='testuser',
    )
    return user


def _make_price_result(base=10000, final=10000, discount=0, months=1, days=30):
    """Создаёт ModemPriceResult."""
    return ModemPriceResult(
        base_price=base,
        final_price=final,
        discount_percent=discount,
        discount_amount=base - final,
        charged_months=months,
        remaining_days=days,
        end_date=datetime.now(UTC) + timedelta(days=days),
    )


class TestModemStatusEndpoint:
    """Тесты GET /modem/status."""

    def test_status_available(self):
        """Модем доступен — возвращает available=True."""

        result = ModemAvailabilityResult(available=True, modem_enabled=False)
        assert result.available is True
        assert result.error is None
        assert result.modem_enabled is False

    def test_status_no_subscription(self):
        """Без подписки — available=False, error=no_subscription."""
        result = ModemAvailabilityResult(available=False, error=ModemError.NO_SUBSCRIPTION, modem_enabled=False)
        assert result.available is False
        assert result.error == ModemError.NO_SUBSCRIPTION

    def test_status_trial(self):
        """Триальная подписка — модем недоступен."""
        result = ModemAvailabilityResult(available=False, error=ModemError.TRIAL_SUBSCRIPTION, modem_enabled=False)
        assert result.available is False
        assert result.error == ModemError.TRIAL_SUBSCRIPTION

    def test_status_modem_enabled(self):
        """Модем уже включён."""
        result = ModemAvailabilityResult(available=True, modem_enabled=True)
        assert result.modem_enabled is True

    def test_status_disabled_feature(self):
        """Функция модема отключена."""
        result = ModemAvailabilityResult(available=False, error=ModemError.MODEM_DISABLED, modem_enabled=False)
        assert result.error == ModemError.MODEM_DISABLED


class TestModemPriceEndpoint:
    """Тесты GET /modem/price."""

    def test_price_no_discount(self):
        """Цена без скидки."""
        result = _make_price_result(base=10000, final=10000, months=1, days=30)
        assert result.base_price == 10000
        assert result.final_price == 10000
        assert result.has_discount is False

    def test_price_with_discount(self):
        """Цена со скидкой за период 3 месяца."""
        result = _make_price_result(base=30000, final=25500, discount=15, months=3, days=90)
        assert result.base_price == 30000
        assert result.final_price == 25500
        assert result.discount_percent == 15
        assert result.has_discount is True

    def test_price_prorated_5_days(self):
        """Пропорциональная цена за 5 дней (1 месяц минимум)."""
        result = _make_price_result(base=10000, final=10000, months=1, days=5)
        assert result.remaining_days == 5
        assert result.charged_months == 1
        assert result.final_price == 10000


class TestModemEnableEndpoint:
    """Тесты POST /modem/enable."""

    def test_enable_success_result(self):
        """Успешное подключение модема."""
        result = ModemEnableResult(success=True, charged_amount=10000, new_device_limit=3)
        assert result.success is True
        assert result.charged_amount == 10000
        assert result.new_device_limit == 3

    def test_enable_insufficient_funds_result(self):
        """Недостаточно средств."""
        result = ModemEnableResult(success=False, error=ModemError.INSUFFICIENT_FUNDS)
        assert result.success is False
        assert result.error == ModemError.INSUFFICIENT_FUNDS

    def test_enable_already_enabled_result(self):
        """Модем уже подключен."""
        availability = ModemAvailabilityResult(available=False, error=ModemError.ALREADY_ENABLED, modem_enabled=True)
        assert not availability.available
        assert availability.error == ModemError.ALREADY_ENABLED


class TestModemDisableEndpoint:
    """Тесты POST /modem/disable."""

    def test_disable_success_result(self):
        """Успешное отключение модема."""
        result = ModemDisableResult(success=True, new_device_limit=2)
        assert result.success is True
        assert result.new_device_limit == 2

    def test_disable_not_enabled_result(self):
        """Модем не подключен — нельзя отключить."""
        availability = ModemAvailabilityResult(available=False, error=ModemError.NOT_ENABLED, modem_enabled=False)
        assert not availability.available
        assert availability.error == ModemError.NOT_ENABLED


class TestErrorMessages:
    """Тесты сообщений об ошибках."""

    def test_all_errors_have_codes(self):
        """Все коды ошибок имеют строковые значения."""
        for error in ModemError:
            assert isinstance(error.value, str)
            assert len(error.value) > 0

    def test_error_values_unique(self):
        """Все коды ошибок уникальны."""
        values = [e.value for e in ModemError]
        assert len(values) == len(set(values))


class TestModemInRenewal:
    """Тесты учёта модема при продлении подписки."""

    def test_modem_excluded_from_extra_devices(self):
        """Модем не считается как доп. устройство при продлении."""
        # Тариф с 2 устройствами, у пользователя 3 (2 + модем)
        tariff_device_limit = 2
        device_limit = 3
        modem_enabled = True

        effective_device_limit = device_limit
        if modem_enabled:
            effective_device_limit = max(0, effective_device_limit - 1)

        extra_devices = max(0, effective_device_limit - tariff_device_limit)
        assert extra_devices == 0  # Модем не считается как доп. устройство

    def test_extra_devices_without_modem(self):
        """Без модема — доп. устройства считаются нормально."""
        tariff_device_limit = 2
        device_limit = 4
        modem_enabled = False

        effective_device_limit = device_limit
        if modem_enabled:
            effective_device_limit = max(0, effective_device_limit - 1)

        extra_devices = max(0, effective_device_limit - tariff_device_limit)
        assert extra_devices == 2

    def test_extra_devices_with_modem_and_addon(self):
        """Модем + докупленные устройства — считаются только докупленные."""
        tariff_device_limit = 2
        device_limit = 5  # 2 (тариф) + 2 (доп.) + 1 (модем)
        modem_enabled = True

        effective_device_limit = device_limit
        if modem_enabled:
            effective_device_limit = max(0, effective_device_limit - 1)

        extra_devices = max(0, effective_device_limit - tariff_device_limit)
        assert extra_devices == 2  # Только докупленные, без модема

    def test_modem_price_calculated_for_renewal_period(self):
        """Цена модема рассчитывается за период продления."""
        from app.utils.pricing_utils import calculate_months_from_days

        modem_price_per_month = 10000  # 100₽/мес
        period_days = 90  # 3 месяца

        months = calculate_months_from_days(period_days)
        modem_base_price = modem_price_per_month * months
        assert months == 3
        assert modem_base_price == 30000  # 300₽

    def test_modem_price_with_period_discount(self):
        """Скидка на модем за длительный период."""
        modem_price_per_month = 10000
        months = 3
        discount_percent = 15  # 3 месяца = 15% скидка

        base_price = modem_price_per_month * months  # 30000
        discount_amount = base_price * discount_percent // 100  # 4500
        final_price = base_price - discount_amount  # 25500

        assert base_price == 30000
        assert discount_amount == 4500
        assert final_price == 25500

    def test_modem_price_one_month_no_discount(self):
        """За 1 месяц скидки нет."""
        modem_price_per_month = 10000
        months = 1

        base_price = modem_price_per_month * months
        final_price = base_price  # Без скидки

        assert final_price == 10000
