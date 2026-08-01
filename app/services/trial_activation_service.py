from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import decrement_subscription_server_counts
from app.database.crud.transaction import create_transaction
from app.database.crud.user import add_user_balance, subtract_user_balance
from app.database.models import PaymentMethod, Subscription, TransactionType, User
from app.services.subscription_service import SubscriptionService
from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

if TYPE_CHECKING:
    from aiogram import Bot


logger = structlog.get_logger(__name__)


class TrialPaymentError(Exception):
    """Base exception for trial activation payment issues."""


@dataclass(slots=True)
class TrialPaymentInsufficientFunds(TrialPaymentError):
    required_amount: int
    balance_amount: int

    @property
    def missing_amount(self) -> int:
        return max(0, self.required_amount - self.balance_amount)


class TrialPaymentChargeFailed(TrialPaymentError):
    """Raised when balance charge could not be completed."""


@dataclass(slots=True)
class TrialActivationReversionResult:
    refunded: bool = True
    subscription_rolled_back: bool = True


def get_trial_activation_charge_amount() -> int:
    """Returns the configured activation charge in kopeks if payment is enabled."""

    if not settings.is_trial_paid_activation_enabled():
        return 0

    try:
        price_kopeks = int(settings.get_trial_activation_price() or 0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        price_kopeks = 0

    return max(0, price_kopeks)


def preview_trial_activation_charge(user: User) -> int:
    """Validates that the user can afford the trial activation charge."""

    price_kopeks = get_trial_activation_charge_amount()
    if price_kopeks <= 0:
        return 0

    balance = int(getattr(user, 'balance_kopeks', 0) or 0)
    if balance < price_kopeks:
        raise TrialPaymentInsufficientFunds(price_kopeks, balance)

    return price_kopeks


async def charge_trial_activation_if_required(
    db: AsyncSession,
    user: User,
    *,
    description: str | None = None,
) -> int:
    """Charges the user's balance if paid trial activation is enabled.

    Returns the charged amount in kopeks. If payment is not required or the
    configured price is zero, the function returns ``0``.
    """

    price_kopeks = preview_trial_activation_charge(user)
    if price_kopeks <= 0:
        return 0

    charge_description = description or 'Активация триальной подписки'

    success = await subtract_user_balance(
        db,
        user,
        price_kopeks,
        charge_description,
        mark_as_paid_subscription=True,
    )
    if not success:
        raise TrialPaymentChargeFailed

    # Создаём транзакцию для учёта списания за триал
    await create_transaction(
        db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=price_kopeks,
        description=charge_description,
        payment_method=PaymentMethod.BALANCE,
    )

    return int(price_kopeks)


async def refund_trial_activation_charge(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    *,
    description: str | None = None,
) -> bool:
    """Refunds a previously charged trial activation amount back to the user."""

    if amount_kopeks <= 0:
        return True

    refund_description = description or 'Возврат оплаты за активацию триальной подписки'

    success = await add_user_balance(
        db,
        user,
        amount_kopeks,
        refund_description,
        transaction_type=TransactionType.REFUND,
    )

    if not success:
        logger.error(
            'Failed to refund kopeks for user during trial activation rollback',
            amount_kopeks=amount_kopeks,
            getattr=getattr(user, 'id', '<unknown>'),
        )

    return success


async def rollback_trial_subscription_activation(
    db: AsyncSession,
    subscription: Subscription | None,
) -> bool:
    """Attempts to undo a previously created trial subscription.

    Returns ``True`` when the rollback succeeds or when ``subscription`` is
    falsy. In case of a database failure the function returns ``False`` after
    logging the error so callers can decide how to proceed.
    """

    if not subscription:
        return True

    try:
        await decrement_subscription_server_counts(db, subscription)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            'Failed to decrement server counters during trial rollback', user_id=subscription.user_id, error=error
        )

    try:
        await db.delete(subscription)
        await db.commit()
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            'Failed to remove trial subscription after charge failure',
            getattr=getattr(subscription, 'id', '<unknown>'),
            error=error,
        )
        await db.rollback()
        return False

    return True


async def revert_trial_activation(
    db: AsyncSession,
    user: User,
    subscription: Subscription | None,
    charged_amount: int,
    *,
    refund_description: str | None = None,
) -> TrialActivationReversionResult:
    """Rolls back a trial subscription and refunds any charged amount."""

    rollback_success = await rollback_trial_subscription_activation(db, subscription)
    refund_success = await refund_trial_activation_charge(
        db,
        user,
        charged_amount,
        description=refund_description,
    )

    try:
        await db.refresh(user)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.warning(
            'Failed to refresh user after reverting trial activation',
            getattr=getattr(user, 'id', '<unknown>'),
            error=error,
        )

    return TrialActivationReversionResult(
        refunded=refund_success,
        subscription_rolled_back=rollback_success,
    )


async def activate_paid_trial_core(
    db: AsyncSession,
    user: User,
    *,
    bot: Bot | None = None,
    requires_payment: bool = False,
) -> Subscription:
    """Ядро активации платного триала.

    Вычисляет параметры триала (из тарифа или настроек), создаёт подписку,
    синхронизирует пользователя в Remnawave и отправляет уведомления.

    Предусловия (проверяются ВЫЗЫВАТЕЛЕМ, не здесь):
    - пользователь ещё не использовал триал;
    - у пользователя нет активной подписки;
    - тип аутентификации разрешает триал;
    - если нужно — баланс уже списан (charge_trial_activation_if_required).

    ``requires_payment`` используется только для атрибуции суммы в
    административном уведомлении (None vs конкретная сумма); на саму логику
    активации не влияет.

    Возвращает созданный объект :class:`~app.database.models.Subscription`.
    """
    # --- Вычисляем параметры триала (тариф → settings-фолбэк) ---
    trial_duration = settings.TRIAL_DURATION_DAYS
    trial_traffic_limit = settings.TRIAL_TRAFFIC_LIMIT_GB
    trial_device_limit = settings.TRIAL_DEVICE_LIMIT
    trial_squads: list[str] = []
    tariff_id_for_trial: int | None = None

    trial_tariff = None
    try:
        from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff

        trial_tariff = await get_trial_tariff(db)

        if not trial_tariff:
            trial_tariff_id = settings.get_trial_tariff_id()
            if trial_tariff_id > 0:
                # Триальный тариф намеренно может быть НЕактивным (скрыт из списка
                # покупки, но задаёт лимиты триала) — не отбраковываем по is_active.
                trial_tariff = await get_tariff_by_id(db, trial_tariff_id)

        if trial_tariff:
            from app.database.crud.server_squad import get_effective_tariff_squad_uuids

            tariff_traffic = int(trial_tariff.traffic_limit_gb or 0)
            trial_traffic_limit = tariff_traffic if tariff_traffic > 0 else settings.TRIAL_TRAFFIC_LIMIT_GB
            tariff_devices = int(trial_tariff.device_limit or 0)
            trial_device_limit = tariff_devices if tariff_devices > 0 else settings.TRIAL_DEVICE_LIMIT
            trial_squads = await get_effective_tariff_squad_uuids(db, trial_tariff.allowed_squads)
            tariff_id_for_trial = trial_tariff.id
            tariff_trial_days = getattr(trial_tariff, 'trial_duration_days', None)
            if tariff_trial_days:
                trial_duration = tariff_trial_days
            logger.info(
                'Using trial tariff (ID: ) with squads',
                trial_tariff_name=trial_tariff.name,
                trial_tariff_id=trial_tariff.id,
                trial_squads=trial_squads,
            )
    except Exception as e:
        logger.error('Error getting trial tariff', error=e)

    # Фолбэк: рандомный squad, если тариф не задал ни одного.
    if not trial_squads:
        from app.database.crud.server_squad import get_random_trial_squad_uuid

        trial_squad_uuid = await get_random_trial_squad_uuid(db)
        trial_squads = [trial_squad_uuid] if trial_squad_uuid else []

    # --- Создаём подписку ---
    from app.database.crud.subscription import create_trial_subscription

    subscription = await create_trial_subscription(
        db=db,
        user_id=user.id,
        duration_days=trial_duration,
        traffic_limit_gb=trial_traffic_limit,
        device_limit=trial_device_limit,
        connected_squads=trial_squads or None,
        tariff_id=tariff_id_for_trial,
    )

    logger.info('Trial subscription activated for user', user_id=user.id)

    # --- Синхронизация с Remnawave ---
    subscription_service = SubscriptionService()
    panel_user = None
    try:
        if subscription_service.is_configured:
            panel_user = await subscription_service.create_remnawave_user(db, subscription)
            await db.refresh(subscription)
    except Exception as e:
        logger.error('Failed to create RemnaWave user for trial', error=e)

    # create_remnawave_user проглатывает RemnaWaveAPIError внутри себя и
    # возвращает None — без явной проверки подписка «активна» без subscription_url.
    if subscription_service.is_configured and panel_user is None:
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='create',
        )
        logger.warning(
            'Trial RemnaWave user not provisioned, enqueued for retry',
            user_id=user.id,
            subscription_id=subscription.id,
        )

    # --- Административное уведомление (фоновая задача) ---
    try:
        captured_user_id = user.id
        captured_sub_id = subscription.id
        captured_amount = settings.TRIAL_ACTIVATION_PRICE if requires_payment else None

        async def _trial_notify(svc, bg_db):
            from app.database.crud.subscription import get_subscription_by_id
            from app.database.crud.user import get_user_by_id

            u = await get_user_by_id(bg_db, captured_user_id)
            s = await get_subscription_by_id(bg_db, captured_sub_id)
            if u and s:
                await svc.send_trial_activation_notification(
                    bg_db, u, s, charged_amount_kopeks=captured_amount
                )

        dispatch_generic_admin_notification_bg(_trial_notify)
    except Exception as e:
        logger.error('Failed to schedule trial activation notification', error=e)

    return subscription
