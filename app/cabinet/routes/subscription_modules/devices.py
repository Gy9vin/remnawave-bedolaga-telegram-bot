"""Device management endpoints.

GET /subscription/devices
POST /subscription/devices (legacy)
DELETE /subscription/devices/{hwid}
DELETE /subscription/devices
POST /subscription/devices/purchase
GET /subscription/devices/reduction-info
POST /subscription/devices/reduce
GET /subscription/devices/price
POST /subscription/devices/save-cart
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.utils.device_ownership import verify_hwid_belongs_to_user
from app.cabinet.utils.panel_identity import ensure_panel_user_id, resolve_panel_user_id
from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.user import add_user_balance
from app.database.crud.user_device_alias import (
    delete_alias,
    get_aliases_for_user,
    normalize_alias,
    set_alias,
)
from app.database.models import Subscription, TransactionType, User
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.utils.device_refund import calculate_device_refund_kopeks
from app.utils.subscription_utils import resolve_min_device_limit

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import (
    DeleteDevicesBatchRequest,
    DevicePurchaseRequest,
    ReduceDevicesRequest,
)
from .helpers import _apply_addon_discount, resolve_subscription


logger = structlog.get_logger(__name__)

# Cap inline RemnaWave panel sync on user-facing cabinet requests. The product is
# committed before the sync, so a slow/unavailable panel must not hold the HTTP
# response open (the cabinet pay button is bound to the request and would spin
# after delivery). Past this budget the sync is deferred to remnawave_retry_queue.
REMNAWAVE_SYNC_TIMEOUT = 10.0

router = APIRouter()


# Резолв панельной идентичности переехал в app/cabinet/utils/panel_identity.py:
# ровно та же слепота (пустой remnawave_id -> пустой список устройств) вылезла
# в админской части кабинета, и держать вторую копию логики значило чинить её
# дважды. Алиасы оставлены, чтобы не трогать вызовы ниже по файлу.
_resolve_panel_user_id = resolve_panel_user_id
_ensure_panel_user_id = ensure_panel_user_id


async def _resolve_device_price_kopeks(db: AsyncSession, subscription: Subscription) -> int:
    """Цена одного места устройства в копейках за базовый период в 30 дней.

    Тарифная цена важнее глобальной: на разных тарифах место стоит по-разному,
    и брать общую настройку значило бы вернуть человеку не те деньги.
    """
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        tariff_price = getattr(tariff, 'device_price_kopeks', None) if tariff else None
        if isinstance(tariff_price, int) and not isinstance(tariff_price, bool) and tariff_price > 0:
            return tariff_price
    fallback = getattr(settings, 'PRICE_PER_DEVICE', 0)
    return fallback if isinstance(fallback, int) and fallback > 0 else 0


def extract_client_name(user_agent: object) -> str | None:
    """Достать читаемое имя программы из user-agent устройства.

    Панель отдаёт агент целиком — «Happ/2.1.0 (iPhone; iOS 17.4)». Человеку
    нужно только имя: версия ему ничего не говорит, а платформа и так показана
    отдельным полем. Берём часть до первого слэша или пробела.

    Неразборчивый агент даёт None, а не строку «Unknown»: пустое место в
    интерфейсе честнее выдуманного имени, и фронт в этом случае показывает
    платформу с моделью.
    """
    if not isinstance(user_agent, str):
        return None
    name = user_agent.strip().split('/', 1)[0].split(' ', 1)[0].strip()
    return name or None


@router.post('/devices')
async def purchase_devices_legacy(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Purchase additional device slots (legacy endpoint).

    DEPRECATED: Use /devices/purchase instead for full tariff and discount support.
    Now uses tariff-aware pricing when subscription has a tariff_id.
    """
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription purchases are restricted for this account',
        )

    # Resolve subscription (ownership validated), then lock the row for concurrent safety
    resolved = await resolve_subscription(db, user, subscription_id)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    result = await db.execute(
        select(Subscription)
        .where(and_(Subscription.id == resolved.id, Subscription.user_id == user.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if subscription.status not in ['active', 'trial']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Ваша подписка неактивна',
        )

    # Get tariff for device price (if exists)
    tariff = None
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Determine device price and max limit from tariff or settings
    if tariff and tariff.device_price_kopeks is not None:
        device_price = tariff.device_price_kopeks
        max_device_limit = tariff.max_device_limit
    else:
        device_price = settings.PRICE_PER_DEVICE
        max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

    if not device_price or device_price <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Докупка устройств недоступна',
        )
    # device_limit == 0 means unlimited devices (panel limitBypassed=true) —
    # buying additional slots on top of unlimited is meaningless.
    if subscription.device_limit == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='У вас уже безлимит устройств, докупка не требуется',
        )

    # Устройства в пределах тарифного лимита — бесплатные
    current_devices = 1 if subscription.device_limit is None else subscription.device_limit
    if tariff:
        tariff_included = tariff.device_limit or 0
        if current_devices < tariff_included:
            free_devices = tariff_included - current_devices
            chargeable_devices = max(0, request.devices - free_devices)
        else:
            chargeable_devices = request.devices
    else:
        free_baseline = settings.DEFAULT_DEVICE_LIMIT
        if current_devices < free_baseline:
            free_devices = free_baseline - current_devices
            chargeable_devices = max(0, request.devices - free_devices)
        else:
            chargeable_devices = request.devices

    # Прорейт по фактическому остатку подписки — как трафик/серверы, без потолка.
    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    days_left = max(1, math.ceil((end_date - now).total_seconds() / 86400))
    base_total_price = int(device_price * chargeable_devices * days_left / 30)
    if chargeable_devices > 0:
        base_total_price = max(100, base_total_price)  # Минимум 1 рубль

    # Lock user row to prevent TOCTOU on promo-offer state
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Apply discount from promo group
    discount_result = _apply_addon_discount(user, 'devices', base_total_price, days_left)
    total_price = discount_result['discounted']
    devices_discount_percent = discount_result['percent']

    # Ensure minimum price after discount (except for 100% discount)
    if devices_discount_percent < 100 and total_price > 0:
        total_price = max(100, total_price)

    # Check max devices limit (under row lock — prevents concurrent purchases exceeding limit)
    current_devices = 1 if subscription.device_limit is None else subscription.device_limit
    new_devices = current_devices + request.devices

    if max_device_limit and new_devices > max_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Максимальное количество устройств: {max_device_limit}',
        )

    # Check balance (skip for 100% discount)
    if total_price > 0 and user.balance_kopeks < total_price:
        missing = total_price - user.balance_kopeks

        # Сохраняем корзину для автопокупки после пополнения
        try:
            cart_data = {
                'cart_mode': 'add_devices',
                'devices_to_add': request.devices,
                'price_kopeks': total_price,
                'base_price_kopeks': base_total_price,
                'discount_percent': devices_discount_percent,
                'source': 'cabinet',
            }
            await user_cart_service.save_user_cart(user.id, cart_data)
            logger.info(
                'Cart saved for device purchase (cabinet /devices) user + devices',
                user_id=user.id,
                devices=request.devices,
            )
        except Exception as e:
            logger.error('Error saving cart for device purchase (cabinet /devices)', error=e)

        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_funds',
                'error': 'Insufficient balance',
                'required_kopeks': total_price,
                'current_kopeks': user.balance_kopeks,
                'missing_kopeks': missing,
                'cart_saved': True,
            },
        )

    # Deduct balance and create transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import PaymentMethod

    # Build description with discount info
    if devices_discount_percent > 0:
        description = f'Покупка {request.devices} доп. устройств (скидка {devices_discount_percent}%)'
    else:
        description = f'Покупка {request.devices} доп. устройств'

    success = await subtract_user_balance(
        db=db,
        user=user,
        amount_kopeks=total_price,
        description=description,
        create_transaction=True,
        payment_method=PaymentMethod.BALANCE,
        transaction_type=TransactionType.SUBSCRIPTION_PAYMENT,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail='Insufficient funds',
        )

    # Re-lock subscription after subtract_user_balance committed (which released all locks).
    # Re-validate max device limit to prevent concurrent purchases exceeding the limit.
    relock_result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = relock_result.scalar_one()

    actual_current = 1 if subscription.device_limit is None else subscription.device_limit
    actual_new = actual_current + request.devices
    if max_device_limit and actual_new > max_device_limit:
        # Concurrent purchase already exceeded limit — refund balance
        user_refund = await db.execute(
            select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
        )
        refund_user = user_refund.scalar_one()
        refund_user.balance_kopeks += total_price
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Максимальное количество устройств: {max_device_limit}. Баланс возвращён.',
        )

    # Add devices (under lock)
    subscription.device_limit = actual_new
    await db.commit()
    await db.refresh(subscription)
    await db.refresh(user)

    # Sync with RemnaWave (time-bounded — see REMNAWAVE_SYNC_TIMEOUT; product is
    # already committed, defer slow syncs to remnawave_retry_queue).
    try:
        service = SubscriptionService()
        if settings.is_multi_tariff_enabled():
            _should_create = not subscription.remnawave_id
        else:
            _should_create = not getattr(user, 'remnawave_id', None)

        async with asyncio.timeout(REMNAWAVE_SYNC_TIMEOUT):
            if _should_create:
                await service.create_remnawave_user(db, subscription)
            else:
                await service.update_remnawave_user(db, subscription)
    except Exception as e:
        logger.error('Failed to sync devices with RemnaWave (legacy endpoint)', error=e)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='create' if _should_create else 'update',
        )

    # Отправляем уведомление админам (в фоне)
    try:
        from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

        captured_user_id = user.id
        captured_sub_id = subscription.id
        captured_old = current_devices
        captured_new = actual_new
        captured_price = total_price

        async def _devices_notify(svc, bg_db):
            from app.database.crud.subscription import get_subscription_by_id
            from app.database.crud.user import get_user_by_id

            u = await get_user_by_id(bg_db, captured_user_id)
            s = await get_subscription_by_id(bg_db, captured_sub_id)
            if u and s:
                await svc.send_subscription_update_notification(
                    db=bg_db,
                    user=u,
                    subscription=s,
                    update_type='devices',
                    old_value=captured_old,
                    new_value=captured_new,
                    price_paid=captured_price,
                )

        dispatch_generic_admin_notification_bg(_devices_notify)
    except Exception as e:
        logger.error('Failed to schedule admin notification for device purchase', error=e)

    response: dict[str, Any] = {
        'message': 'Devices added successfully',
        'devices_added': request.devices,
        'new_device_limit': actual_new,
        'amount_paid_kopeks': total_price,
    }

    if devices_discount_percent > 0:
        response['discount_percent'] = devices_discount_percent
        response['discount_kopeks'] = discount_result['discount']
        response['base_price_kopeks'] = base_total_price

    return response


@router.post('/devices/purchase')
async def purchase_devices(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Purchase additional device slots for subscription."""
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription purchases are restricted for this account',
        )

    try:
        # Resolve subscription (ownership validated), then lock the row for concurrent safety
        resolved = await resolve_subscription(db, user, subscription_id)
        if not resolved:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='У вас нет активной подписки')

        result = await db.execute(
            select(Subscription)
            .where(and_(Subscription.id == resolved.id, Subscription.user_id == user.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        subscription = result.scalar_one_or_none()

        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='У вас нет активной подписки',
            )

        if subscription.status not in ['active', 'trial']:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Ваша подписка неактивна',
            )

        # Get tariff for device price (if exists)
        tariff = None
        if subscription.tariff_id:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, subscription.tariff_id)

        # Determine device price and max limit from tariff or settings
        if tariff and tariff.device_price_kopeks is not None:
            device_price = tariff.device_price_kopeks
            max_device_limit = tariff.max_device_limit
        else:
            # Classic mode - use settings
            device_price = settings.PRICE_PER_DEVICE
            max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

        if not device_price or device_price <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Докупка устройств недоступна',
            )
        # device_limit == 0 means unlimited devices (panel limitBypassed=true) —
        # buying additional slots on top of unlimited is meaningless.
        if subscription.device_limit == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='У вас уже безлимит устройств, докупка не требуется',
            )

        # Check max device limit (under row lock — prevents concurrent purchases exceeding limit)
        current_devices = 1 if subscription.device_limit is None else subscription.device_limit
        new_device_count = current_devices + request.devices
        if max_device_limit and new_device_count > max_device_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Максимальное количество устройств: {max_device_limit}',
            )

        # Calculate prorated price based on remaining days
        now = datetime.now(UTC)
        end_date = subscription.end_date
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)

        days_left = max(1, math.ceil((end_date - now).total_seconds() / 86400))
        total_days = 30  # Base period for device price calculation
        # Прорейт по фактическому остатку подписки — как трафик/серверы, без потолка.
        # Устройство активно до конца подписки; на продлении доначисляется через
        # pricing_engine. (Раньше тут был потолок в 1 месяц — #596757/#587412.)
        effective_days = days_left

        # Устройства в пределах тарифного лимита — бесплатные
        if tariff:
            tariff_included = tariff.device_limit or 0
            if current_devices < tariff_included:
                free_devices = tariff_included - current_devices
                chargeable_devices = max(0, request.devices - free_devices)
            else:
                chargeable_devices = request.devices
        else:
            free_baseline = settings.DEFAULT_DEVICE_LIMIT
            if current_devices < free_baseline:
                free_devices = free_baseline - current_devices
                chargeable_devices = max(0, request.devices - free_devices)
            else:
                chargeable_devices = request.devices

        # Calculate base price before discount
        base_price_per_month = device_price * chargeable_devices
        base_price_prorated = int(base_price_per_month * effective_days / total_days)
        if chargeable_devices > 0:
            base_price_prorated = max(100, base_price_prorated)  # Minimum 1 ruble

        # Lock user BEFORE discount computation to prevent TOCTOU on promo group
        from app.database.crud.user import lock_user_for_pricing

        user = await lock_user_for_pricing(db, user.id)

        # Apply discount from promo group
        period_hint_days = days_left
        discount_result = _apply_addon_discount(user, 'devices', base_price_prorated, period_hint_days)
        price_kopeks = discount_result['discounted']
        devices_discount_percent = discount_result['percent']
        discount_value = discount_result['discount']

        # Ensure minimum price after discount (except for 100% discount)
        if devices_discount_percent < 100:
            price_kopeks = max(100, price_kopeks)

        # Check balance (skip for 100% discount)
        if price_kopeks > 0 and user.balance_kopeks < price_kopeks:
            missing = price_kopeks - user.balance_kopeks

            # Сохраняем корзину для автопокупки после пополнения
            try:
                cart_data = {
                    'cart_mode': 'add_devices',
                    'devices_to_add': request.devices,
                    'price_kopeks': price_kopeks,
                    'base_price_kopeks': base_price_prorated,
                    'discount_percent': devices_discount_percent,
                    'source': 'cabinet',
                }
                await user_cart_service.save_user_cart(user.id, cart_data)
                logger.info(
                    'Cart saved for device purchase (cabinet) user + devices, discount',
                    user_id=user.id,
                    devices=request.devices,
                    devices_discount_percent=devices_discount_percent,
                )
            except Exception as e:
                logger.error('Error saving cart for device purchase (cabinet)', error=e)

            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    'code': 'insufficient_funds',
                    'error': 'Insufficient balance',
                    'required_kopeks': price_kopeks,
                    'current_kopeks': user.balance_kopeks,
                    'missing_kopeks': missing,
                    'cart_saved': True,
                },
            )

        # Deduct balance and create transaction
        from app.database.crud.user import subtract_user_balance
        from app.database.models import PaymentMethod

        # Build description with discount info
        if devices_discount_percent > 0:
            description = f'Покупка {request.devices} доп. устройств (скидка {devices_discount_percent}%)'
        else:
            description = f'Покупка {request.devices} доп. устройств'

        success = await subtract_user_balance(
            db=db,
            user=user,
            amount_kopeks=price_kopeks,
            description=description,
            create_transaction=True,
            payment_method=PaymentMethod.BALANCE,
            transaction_type=TransactionType.SUBSCRIPTION_PAYMENT,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail='Insufficient funds',
            )

        # Re-lock subscription after subtract_user_balance committed (which released all locks).
        # Re-validate max device limit to prevent concurrent purchases exceeding the limit.
        relock_result = await db.execute(
            select(Subscription)
            .where(Subscription.id == subscription.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        subscription = relock_result.scalar_one()

        actual_current = 1 if subscription.device_limit is None else subscription.device_limit
        actual_new = actual_current + request.devices
        if max_device_limit and actual_new > max_device_limit:
            # Concurrent purchase already exceeded limit — refund balance
            user_refund = await db.execute(
                select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
            )
            refund_user = user_refund.scalar_one()
            refund_user.balance_kopeks += price_kopeks
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'Максимальное количество устройств: {max_device_limit}. Баланс возвращён.',
            )

        # Increase device limit (under lock)
        subscription.device_limit = actual_new
        await db.commit()
        await db.refresh(subscription)

        # Sync with RemnaWave (time-bounded — see REMNAWAVE_SYNC_TIMEOUT; product is
        # already committed, defer slow syncs to remnawave_retry_queue).
        service = SubscriptionService()
        try:
            if settings.is_multi_tariff_enabled():
                _should_create = not subscription.remnawave_id
            else:
                _should_create = not getattr(user, 'remnawave_id', None)

            async with asyncio.timeout(REMNAWAVE_SYNC_TIMEOUT):
                if _should_create:
                    await service.create_remnawave_user(db, subscription)
                else:
                    await service.update_remnawave_user(db, subscription)
        except Exception as e:
            logger.error('Failed to sync devices with RemnaWave', error=e)
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=user.id,
                action='create' if _should_create else 'update',
            )

        await db.refresh(user)

        if devices_discount_percent > 0:
            logger.info(
                'User purchased devices for kopeks (discount saved kopeks)',
                user_id=user.id,
                devices=request.devices,
                price_kopeks=price_kopeks,
                devices_discount_percent=devices_discount_percent,
                discount_value=discount_value,
            )
        else:
            logger.info(
                'User purchased devices for kopeks', user_id=user.id, devices=request.devices, price_kopeks=price_kopeks
            )

        # Отправляем уведомление админам (в фоне)
        try:
            from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

            captured_user_id = user.id
            captured_sub_id = subscription.id
            captured_old = current_devices
            captured_new = subscription.device_limit
            captured_price = price_kopeks

            async def _devices_notify(svc, bg_db):
                from app.database.crud.subscription import get_subscription_by_id
                from app.database.crud.user import get_user_by_id

                u = await get_user_by_id(bg_db, captured_user_id)
                s = await get_subscription_by_id(bg_db, captured_sub_id)
                if u and s:
                    await svc.send_subscription_update_notification(
                        db=bg_db,
                        user=u,
                        subscription=s,
                        update_type='devices',
                        old_value=captured_old,
                        new_value=captured_new,
                        price_paid=captured_price,
                    )

            dispatch_generic_admin_notification_bg(_devices_notify)
        except Exception as e:
            logger.error('Failed to schedule admin notification for device purchase', error=e)

        # Yandex.Metrika offline conversion (#558449).
        try:
            from app.services import yandex_offline_conv_service as yandex_conv

            # Purchase event fires centrally from create_transaction; here we
            # only persist the request-body CID synchronously (#558449).
            await yandex_conv.store_cid_only(
                user.id,
                request.yandex_cid,
            )
        except Exception as yconv_err:
            logger.debug('yandex_conv purchase hook failed (non-fatal)', user_id=user.id, error=str(yconv_err))

        response: dict[str, Any] = {
            'success': True,
            'message': f'Добавлено {request.devices} устройств',
            'devices_added': request.devices,
            'new_device_limit': subscription.device_limit,
            'price_kopeks': price_kopeks,
            'price_label': settings.format_price(price_kopeks),
            'balance_kopeks': user.balance_kopeks,
            'balance_label': settings.format_price(user.balance_kopeks),
        }

        if devices_discount_percent > 0:
            response['discount_percent'] = devices_discount_percent
            response['discount_kopeks'] = discount_value
            response['base_price_kopeks'] = base_price_prorated

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to purchase devices for user', user_id=user.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Не удалось обработать покупку устройств',
        )


@router.post('/devices/save-cart')
async def save_devices_cart(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, bool]:
    """Save cart for device purchase (for insufficient balance flow)."""
    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='У вас нет активной подписки',
        )

    if subscription.status not in ['active', 'trial']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Ваша подписка неактивна',
        )

    # Get tariff for device price (if exists)
    tariff = None
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Determine device price and max limit from tariff or settings
    if tariff and tariff.device_price_kopeks is not None:
        device_price = tariff.device_price_kopeks
        max_device_limit = tariff.max_device_limit
    else:
        device_price = settings.PRICE_PER_DEVICE
        max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

    if not device_price or device_price <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Докупка устройств недоступна',
        )
    # device_limit == 0 means unlimited devices (panel limitBypassed=true) —
    # buying additional slots on top of unlimited is meaningless.
    if subscription.device_limit == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='У вас уже безлимит устройств, докупка не требуется',
        )


    # Check max device limit
    current_devices = 1 if subscription.device_limit is None else subscription.device_limit
    new_device_count = current_devices + request.devices
    if max_device_limit and new_device_count > max_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Максимальное количество устройств: {max_device_limit}',
        )

    # Calculate prorated price based on remaining days
    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)

    days_left = max(1, math.ceil((end_date - now).total_seconds() / 86400))
    total_days = 30
    # Прорейт по фактическому остатку подписки — как трафик/серверы, без потолка
    # (раньше был потолок в 1 месяц — #596757). Доначисление за устройства — на продлении.
    effective_days = days_left

    # Устройства в пределах тарифного лимита — бесплатные
    if tariff:
        tariff_included = tariff.device_limit or 0
        if current_devices < tariff_included:
            free_devices = tariff_included - current_devices
            chargeable_devices = max(0, request.devices - free_devices)
        else:
            chargeable_devices = request.devices
    else:
        free_baseline = settings.DEFAULT_DEVICE_LIMIT
        if current_devices < free_baseline:
            free_devices = free_baseline - current_devices
            chargeable_devices = max(0, request.devices - free_devices)
        else:
            chargeable_devices = request.devices

    base_total_price = int(device_price * chargeable_devices * effective_days / total_days)
    if chargeable_devices > 0:
        base_total_price = max(100, base_total_price)  # Minimum 1 ruble

    # Apply discount from promo group
    period_hint_days = days_left
    discount_result = _apply_addon_discount(user, 'devices', base_total_price, period_hint_days)
    price_kopeks = discount_result['discounted']
    devices_discount_percent = discount_result['percent']

    # Ensure minimum price after discount (except for 100% discount)
    if devices_discount_percent < 100 and price_kopeks > 0:
        price_kopeks = max(100, price_kopeks)

    # Save cart for auto-purchase after balance top-up
    cart_data = {
        'cart_mode': 'add_devices',
        'devices_to_add': request.devices,
        'price_kopeks': price_kopeks,
        'base_price_kopeks': base_total_price,
        'discount_percent': devices_discount_percent,
        'source': 'cabinet',
    }
    await user_cart_service.save_user_cart(user.id, cart_data)
    logger.info(
        'Cart saved for device purchase (cabinet save-cart) user + devices', user_id=user.id, devices=request.devices
    )

    return {'success': True, 'cart_saved': True}


@router.get('/devices/price')
async def get_device_price(
    devices: int = 1,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get price for additional devices."""
    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription or subscription.status not in ['active', 'trial']:
        return {
            'available': False,
            'reason': 'Нет активной подписки',
        }

    tariff = None
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Determine device price and max limit from tariff or settings
    if tariff and tariff.device_price_kopeks is not None:
        device_price = tariff.device_price_kopeks
        max_device_limit = tariff.max_device_limit
    else:
        # Classic mode - use settings
        device_price = settings.PRICE_PER_DEVICE
        max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

    if not device_price or device_price <= 0:
        return {
            'available': False,
            'reason': 'Докупка устройств недоступна',
        }
    # device_limit == 0 means unlimited devices (panel limitBypassed=true) —
    # buying additional slots on top of unlimited is meaningless.
    if subscription.device_limit == 0:
        return {
            'available': False,
            'reason': 'У вас уже безлимит устройств, докупка не требуется',
        }


    # Check max device limit
    current_devices = 1 if subscription.device_limit is None else subscription.device_limit
    can_add = max_device_limit - current_devices if max_device_limit else None

    if max_device_limit and current_devices >= max_device_limit:
        return {
            'available': False,
            'reason': f'Достигнут максимум устройств ({max_device_limit})',
            'current_device_limit': current_devices,
            'max_device_limit': max_device_limit,
        }

    if max_device_limit and current_devices + devices > max_device_limit:
        return {
            'available': False,
            'reason': f'Можно добавить максимум {can_add} устройств',
            'current_device_limit': current_devices,
            'max_device_limit': max_device_limit,
            'can_add': can_add,
        }

    # Calculate prorated price
    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)

    days_left = max(1, math.ceil((end_date - now).total_seconds() / 86400))
    total_days = 30
    # Прорейт по фактическому остатку подписки — как трафик/серверы, без потолка
    # (раньше был потолок в 1 месяц — #596757). Доначисление за устройства — на продлении.
    effective_days = days_left

    # Устройства в пределах тарифного лимита — бесплатные
    if tariff:
        tariff_included = tariff.device_limit or 0
        if current_devices < tariff_included:
            free_devices = tariff_included - current_devices
            chargeable_devices = max(0, devices - free_devices)
        else:
            chargeable_devices = devices
    else:
        free_baseline = settings.DEFAULT_DEVICE_LIMIT
        if current_devices < free_baseline:
            free_devices = free_baseline - current_devices
            chargeable_devices = max(0, devices - free_devices)
        else:
            chargeable_devices = devices

    # Calculate base price before discount (total first, then floor)
    base_total_price = int(device_price * chargeable_devices * effective_days / total_days)
    if chargeable_devices > 0:
        base_total_price = max(100, base_total_price)

    # Apply discount from promo group
    period_hint_days = days_left
    discount_result = _apply_addon_discount(user, 'devices', base_total_price, period_hint_days)
    total_price_kopeks = discount_result['discounted']
    devices_discount_percent = discount_result['percent']
    discount_value = discount_result['discount']

    # Ensure minimum price after discount (except for 100% discount)
    if devices_discount_percent < 100 and total_price_kopeks > 0:
        total_price_kopeks = max(100, total_price_kopeks)
    price_per_device_kopeks = total_price_kopeks // devices if devices > 0 else 0

    response: dict[str, Any] = {
        'available': True,
        'devices': devices,
        'price_per_device_kopeks': price_per_device_kopeks,
        'price_per_device_label': settings.format_price(price_per_device_kopeks),
        'total_price_kopeks': total_price_kopeks,
        'total_price_label': settings.format_price(total_price_kopeks),
        'current_device_limit': current_devices,
        'max_device_limit': max_device_limit,
        'can_add': can_add,
        'days_left': days_left,
        'base_device_price_kopeks': device_price,
    }

    # Add discount info if applicable
    if devices_discount_percent > 0:
        response['discount_percent'] = devices_discount_percent
        response['discount_kopeks'] = discount_value
        response['base_total_price_kopeks'] = base_total_price
        response['original_price_per_device_kopeks'] = base_total_price // devices if devices > 0 else 0

    return response


# ============ Device Management (list/delete) ============


@router.get('/devices')
async def get_devices(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get list of connected devices."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            # Резолв внутри клиента: если панельный id ещё не сохранён (бэкфилл
            # после перехода на 3.x не прогнан), достаём его из панели на месте,
            # иначе список выглядел бы пустым при живых устройствах.
            _panel_user_id = await _ensure_panel_user_id(db, subscription, user, api)
            if not _panel_user_id:
                # Не гейтуем список устройств тихим пустым списком без следа —
                # раньше решение "у юзера нет панель-аккаунта" не всегда
                # логировалось, что затрудняло диагностику, когда список
                # внезапно пуст. warning тут не мешает деградации (ответ всё
                # равно пустой список), но виден в логах.
                logger.warning(
                    'No panel identity resolved for device list',
                    user_id=user.id,
                    subscription_id=subscription.id,
                )
                return {
                    'devices': [],
                    'total': 0,
                    'device_limit': subscription.device_limit or 0,
                }

            response = await api.get_user_devices_all(_panel_user_id)

            devices_list = response.get('devices', [])
            # Подтягиваем все локальные alias'ы юзера одним запросом — дешевле
            # чем N+1 при сборке списка устройств. Aliases декоративны: при
            # сбое чтения возвращаем список без них, а не 500.
            try:
                aliases = await get_aliases_for_user(db, user.id)
            except Exception as alias_error:
                logger.warning(
                    'Failed to load device aliases, falling back to defaults',
                    user_id=user.id,
                    error=str(alias_error)[:200],
                )
                aliases = {}

            formatted_devices = []
            for device in devices_list:
                hwid = device.get('hwid') or device.get('deviceId') or device.get('id')
                platform = device.get('platform') or device.get('platformType') or 'Unknown'
                model = device.get('deviceModel') or device.get('model') or device.get('name') or 'Unknown'
                created_at = device.get('updatedAt') or device.get('lastSeen') or device.get('createdAt')

                formatted_devices.append(
                    {
                        'hwid': hwid,
                        'platform': platform,
                        'device_model': model,
                        'created_at': created_at,
                        # Имя программы: Happ, INCY и т.д. None — агент не разобрался,
                        # тогда фронт показывает платформу и модель.
                        'client': extract_client_name(device.get('userAgent')),
                        # Локальное имя, заданное юзером. None — алиаса нет,
                        # фронт фоллбэчит на platform/device_model.
                        'local_name': aliases.get(hwid) or None,
                    }
                )

            return {
                'devices': formatted_devices,
                'total': response.get('total', len(formatted_devices)),
                'device_limit': subscription.device_limit or 0,
            }

    except Exception as e:
        # Панель медленная/недоступна — деградируем мягко (пустой список) и логируем
        # WARNING, как соседние читатели устройств (device_ownership, miniapp), чтобы
        # транзиентный таймаут панели не спамил админ-чат ошибками.
        logger.warning('Failed to load devices from RemnaWave (panel slow/unavailable)', error=str(e)[:200])
        return {
            'devices': [],
            'total': 0,
            'device_limit': subscription.device_limit or 0,
        }


class DeviceRenameRequest(BaseModel):
    """Payload for `PATCH /subscription/devices/{hwid}/name`.

    `name` accepts either a non-empty string (set/update) or null/empty
    string (clear the alias and fall back to the default platform/model
    label). Length is capped at ALIAS_MAX_LENGTH on the backend.
    """

    name: str | None = None


# Hwid ownership validation lives in app.cabinet.utils.device_ownership —
# shared between the user-facing rename endpoint below and the admin
# override in app/cabinet/routes/admin_users.py. Keeps both call sites
# from drifting on multi-tariff semantics again.


@router.patch('/devices/{hwid}/name')
async def rename_device(
    hwid: str,
    request: DeviceRenameRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Set/clear a local alias for the user's HWID device.

    Scope is per-(user, hwid), so the alias is visible across ALL of the
    user's subscriptions in multi-tariff mode — same physical device, same
    nickname.

    Empty/null `name` clears the alias and returns `{local_name: null}`.
    """
    # Subscription resolution здесь только для access-проверки: убеждаемся,
    # что юзер действительно владеет устройством через какую-то из своих
    # подписок. Сам alias всё равно глобальный per (user, hwid).
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    hwid = (hwid or '').strip()
    if not hwid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='hwid is required')

    # Guard against orphan rows: only accept rename requests for devices
    # the user actually owns in RemnaWave panel right now. Multi-tariff
    # aware (unions devices across all panel UUIDs the user holds).
    if not await verify_hwid_belongs_to_user(user, hwid):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Device not found on your account',
        )

    normalized = normalize_alias(request.name)
    if normalized:
        saved = await set_alias(db, user.id, hwid, normalized)
        return {'hwid': hwid, 'local_name': saved}

    await delete_alias(db, user.id, hwid)
    return {'hwid': hwid, 'local_name': None}


@router.delete('/devices/{hwid}')
async def delete_device(
    hwid: str,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete a specific device by HWID."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            _panel_user_id = await _ensure_panel_user_id(db, subscription, user, api)
            if not _panel_user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='Panel user not found',
                )
            # Тело запроса в 3.0.0 — {'userId': int, 'hwid': str}; собираем его не
            # руками, а клиентом: он валидирует идентификатор на границе и
            # проверяет, что hwid действительно пропал из ответа панели.
            removed = await api.remove_device(_panel_user_id, hwid)

        if not removed:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to delete device',
            )

        return {
            'success': True,
            'message': 'Device deleted successfully',
            'deleted_hwid': hwid,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error deleting device', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete device',
        )


@router.delete('/devices')
async def delete_all_devices(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete all connected devices."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            _panel_user_id = await _ensure_panel_user_id(db, subscription, user, api)
            if not _panel_user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='Panel user not found',
                )
            # Get all devices first
            response = await api.get_user_devices_all(_panel_user_id)

            if not response:
                return {
                    'success': True,
                    'message': 'No devices to delete',
                    'deleted_count': 0,
                }

            devices_list = response.get('devices', [])
            if not devices_list:
                return {
                    'success': True,
                    'message': 'No devices to delete',
                    'deleted_count': 0,
                }

            # 3.0.0 даёт атомарный `POST /api/hwid/devices/delete-all` — на него
            # переведены все остальные места. Здесь оставался цикл «по одному
            # запросу на устройство», и он ко всему прочему возвращал
            # `success: true` даже когда не удалилось НИ ОДНО устройство.
            total = len(devices_list)
            if not await api.reset_user_devices(_panel_user_id):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail='Failed to delete devices',
                )

            return {
                'success': True,
                'message': f'Deleted {total} devices',
                'deleted_count': total,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error deleting all devices', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete devices',
        )


@router.post('/devices/delete-batch')
async def delete_devices_batch(
    request: DeleteDevicesBatchRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Отключить несколько выбранных устройств одним запросом.

    Метод POST, а не DELETE: тело у DELETE поддерживается не всеми прокси, а
    список hwid в query-строку не помещается.

    Отказ по одному устройству не останавливает остальные и не выдаётся за
    успех: неудачные hwid возвращаются списком, чтобы фронт показал, что именно
    не отключилось, и не соврал человеку.
    """
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    # Дубликаты схлопываем, порядок сохраняем — иначе один и тот же hwid
    # ушёл бы в панель дважды и второй раз вернул бы отказ.
    unique_hwids: list[str] = []
    seen: set[str] = set()
    for hwid in request.hwids:
        if hwid and hwid not in seen:
            seen.add(hwid)
            unique_hwids.append(hwid)

    deleted_count = 0
    failed_hwids: list[str] = []

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            _panel_user_id = await _ensure_panel_user_id(db, subscription, user, api)
            if not _panel_user_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Panel user not found')

            for hwid in unique_hwids:
                try:
                    if await api.remove_device(_panel_user_id, hwid):
                        deleted_count += 1
                    else:
                        failed_hwids.append(hwid)
                except Exception as device_error:
                    logger.error(
                        'Failed to remove device in batch',
                        user_id=user.id,
                        hwid=hwid,
                        error=str(device_error)[:200],
                    )
                    failed_hwids.append(hwid)

        logger.info(
            'Batch device removal finished',
            user_id=user.id,
            requested=len(unique_hwids),
            deleted=deleted_count,
            failed=len(failed_hwids),
        )

        return {
            'success': not failed_hwids,
            'deleted_count': deleted_count,
            'failed_hwids': failed_hwids,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error deleting devices in batch', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete devices',
        )


# ============ Device Reduction ============


@router.get('/devices/reduction-info')
async def get_device_reduction_info(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get info about device limit reduction availability."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        return {
            'available': False,
            'reason': 'No subscription found',
            'current_device_limit': 0,
            'min_device_limit': 1,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # Check if it's a trial subscription
    if subscription.is_trial:
        return {
            'available': False,
            'reason': 'Device reduction is not available for trial subscriptions',
            'current_device_limit': 1 if subscription.device_limit is None else subscription.device_limit,
            'min_device_limit': 1,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # device_limit == 0 means unlimited devices (panel limitBypassed=true) — there is
    # nothing to reduce from unlimited, and min/max math below doesn't apply to it.
    if subscription.device_limit == 0:
        return {
            'available': False,
            'reason': 'Device limit is unlimited',
            'current_device_limit': 0,
            'min_device_limit': 1,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # По умолчанию нижняя граница уменьшения — лимит устройств тарифа
    # (ALLOW_DEVICES_BELOW_TARIFF_LIMIT=True возвращает прежнее поведение с 1).
    # Тариф грузим явно: ленивый доступ к subscription.tariff в async-сессии
    # падает MissingGreenlet.
    _tariff = None
    if subscription.tariff_id:
        _tariff = await get_tariff_by_id(db, subscription.tariff_id)

    min_device_limit = resolve_min_device_limit(_tariff)

    current_device_limit = 1 if subscription.device_limit is None else subscription.device_limit

    # Can't reduce below minimum
    if current_device_limit <= min_device_limit:
        return {
            'available': False,
            'reason': 'Already at minimum device limit',
            'current_device_limit': current_device_limit,
            'min_device_limit': min_device_limit,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # Get connected devices count
    connected_devices_count = 0
    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            _panel_user_id = await _ensure_panel_user_id(db, subscription, user, api)
            if _panel_user_id:
                response = await api.get_user_devices_all(_panel_user_id)
                if response:
                    connected_devices_count = response.get('total', 0)
    except Exception as e:
        logger.warning('Failed to get connected devices count (panel slow/unavailable)', error=str(e)[:200])

    can_reduce = current_device_limit - min_device_limit

    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date is not None and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    days_left = max(0, math.ceil((end_date - now).total_seconds() / 86400)) if end_date else 0

    return {
        'available': True,
        'current_device_limit': current_device_limit,
        'min_device_limit': min_device_limit,
        'can_reduce': can_reduce,
        'connected_devices_count': connected_devices_count,
        'refund_kopeks_per_slot': calculate_device_refund_kopeks(
            await _resolve_device_price_kopeks(db, subscription), slots=1, days_left=days_left
        ),
    }


@router.post('/devices/reduce')
async def reduce_devices(
    request: ReduceDevicesRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Reduce device limit (no refund)."""
    from app.services.remnawave_service import RemnaWaveService

    new_device_limit = request.new_device_limit

    # Resolve subscription (ownership validated), then lock the row for concurrent safety
    resolved = await resolve_subscription(db, user, subscription_id)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    result = await db.execute(
        select(Subscription)
        .where(and_(Subscription.id == resolved.id, Subscription.user_id == user.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Device reduction is not available for trial subscriptions',
        )

    # device_limit == 0 means unlimited devices (panel limitBypassed=true) — there is
    # nothing to reduce from unlimited, and min/max math below doesn't apply to it.
    if subscription.device_limit == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Device limit is unlimited, nothing to reduce',
        )

    # По умолчанию нижняя граница уменьшения — лимит устройств тарифа
    # (ALLOW_DEVICES_BELOW_TARIFF_LIMIT=True возвращает прежнее поведение с 1).
    # Тариф грузим явно: ленивый доступ к subscription.tariff в async-сессии
    # падает MissingGreenlet.
    _tariff = None
    if subscription.tariff_id:
        _tariff = await get_tariff_by_id(db, subscription.tariff_id)

    min_device_limit = resolve_min_device_limit(_tariff)

    current_device_limit = 1 if subscription.device_limit is None else subscription.device_limit

    # Validate new limit
    if new_device_limit >= current_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='New device limit must be less than current limit',
        )

    if new_device_limit < min_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Cannot reduce below minimum device limit ({min_device_limit}) for your tariff',
        )

    # Сколько мест освобождается и сколько устройств надо отключить — РАЗНЫЕ
    # величины. Мест освобождается «старый лимит минус новый». Устройств
    # отключать надо «сколько подключено минус новый лимит», и это число бывает
    # меньше, а бывает нулём, если человек не выбрал весь лимит.
    freed_slots = current_device_limit - new_device_limit

    connected_devices_count = 0
    devices_removed_count = 0
    devices_list: list[dict[str, Any]] = []

    service = RemnaWaveService()
    async with service.get_api_client() as api:
        _panel_user_id = await _ensure_panel_user_id(db, subscription, user, api)
        if _panel_user_id:
            response = await api.get_user_devices_all(_panel_user_id)
            devices_list = (response or {}).get('devices', []) or []
            connected_devices_count = len(devices_list)

        devices_to_remove_count = max(0, connected_devices_count - new_device_limit)
        known_hwids = {d.get('hwid') for d in devices_list if d.get('hwid')}

        if request.hwids_to_remove is not None:
            chosen: list[str] = []
            seen: set[str] = set()
            for hwid in request.hwids_to_remove:
                if hwid and hwid not in seen:
                    seen.add(hwid)
                    chosen.append(hwid)

            unknown = [hwid for hwid in chosen if hwid not in known_hwids]
            if unknown:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Неизвестные устройства: {", ".join(unknown)}',
                )
            if len(chosen) != devices_to_remove_count:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f'Нужно отключить ровно {devices_to_remove_count} устройств, '
                        f'выбрано {len(chosen)}'
                    ),
                )
            devices_to_delete = [d for d in devices_list if d.get('hwid') in seen]
        else:
            # Совместимость со старыми клиентами: выбираем сами. Сортировка по
            # возрастанию активности, срез С НАЧАЛА — отключаем то, чем давно не
            # пользовались. Прежний код срезал с конца и удалял самые свежие
            # устройства, то есть ровно те, что были нужны человеку.
            sorted_devices = sorted(
                devices_list,
                key=lambda d: d.get('updatedAt') or d.get('createdAt') or '\xff',
            )
            devices_to_delete = sorted_devices[:devices_to_remove_count]

        for device in devices_to_delete:
            device_hwid = device.get('hwid')
            if not device_hwid:
                continue
            try:
                if await api.remove_device(_panel_user_id, device_hwid):
                    devices_removed_count += 1
            except Exception as del_error:
                logger.error(
                    'Error removing device during limit reduction',
                    device_hwid=device_hwid,
                    user_id=user.id,
                    error=str(del_error)[:200],
                )

    # Возврат считаем ДО изменения лимита: после присваивания freed_slots уже не
    # восстановить. Формула зеркальна покупке — цена места на оставшийся срок.
    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date is not None and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    days_left = max(0, math.ceil((end_date - now).total_seconds() / 86400)) if end_date else 0
    device_price_kopeks = await _resolve_device_price_kopeks(db, subscription)
    refund_kopeks = calculate_device_refund_kopeks(
        device_price_kopeks, slots=freed_slots, days_left=days_left
    )

    old_device_limit = current_device_limit
    user_id = user.id  # save before potential rollback (expires ORM objects)

    # Update subscription in memory (will be committed by update_remnawave_user on success)
    subscription.device_limit = new_device_limit
    subscription.updated_at = datetime.now(UTC)

    if refund_kopeks > 0:
        # commit=False: деньги и лимит меняются одной транзакцией. Если панель
        # откажет и мы откатимся, возврат откатится вместе с лимитом, иначе у
        # человека остались бы деньги за места, которые он не потерял.
        await add_user_balance(
            db=db,
            user=user,
            amount_kopeks=refund_kopeks,
            description=f'Возврат за {freed_slots} освобождённых мест устройств',
            create_transaction=True,
            transaction_type=TransactionType.REFUND,
            commit=False,
        )

    # Update RemnaWave — commits on success, returns None on failure
    subscription_service = SubscriptionService()
    result = await subscription_service.update_remnawave_user(db, subscription)

    if result is None:
        # RemnaWave update failed — rollback local changes
        await db.rollback()
        logger.error(
            'Failed to update RemnaWave after device limit reduction',
            user_id=user_id,
            old_device_limit=old_device_limit,
            new_device_limit=new_device_limit,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Не удалось обновить VPN-панель. Попробуйте позже.',
        )

    logger.info(
        'User reduced device limit',
        user_id=user_id,
        old_device_limit=old_device_limit,
        new_device_limit=new_device_limit,
        devices_removed=devices_removed_count if devices_removed_count > 0 else None,
    )

    return {
        'success': True,
        'message': 'Device limit reduced successfully'
        + (f' ({devices_removed_count} devices removed)' if devices_removed_count > 0 else ''),
        'old_device_limit': old_device_limit,
        'new_device_limit': new_device_limit,
        'devices_removed': devices_removed_count,
        'refund_kopeks': refund_kopeks,
    }
