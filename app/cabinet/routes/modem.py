"""Cabinet API routes for modem management."""

import logging

from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.admin_notification_service import AdminNotificationService
from app.services.modem_service import ModemError, get_modem_service
from app.services.subscription_purchase_service import validate_user_can_purchase
from app.services.user_cart_service import user_cart_service

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = logging.getLogger(__name__)

router = APIRouter(prefix='/modem', tags=['Cabinet Modem'])

_ERROR_MESSAGES: dict[ModemError, str] = {
    ModemError.NO_SUBSCRIPTION: 'У вас нет активной подписки',
    ModemError.TRIAL_SUBSCRIPTION: 'Модем недоступен для пробных подписок',
    ModemError.MODEM_DISABLED: 'Функция модема отключена',
    ModemError.ALREADY_ENABLED: 'Модем уже подключен',
    ModemError.NOT_ENABLED: 'Модем не подключен',
    ModemError.INSUFFICIENT_FUNDS: 'Недостаточно средств',
    ModemError.CHARGE_ERROR: 'Ошибка списания средств',
    ModemError.UPDATE_ERROR: 'Ошибка обновления подписки',
}


def _error_detail(error: ModemError) -> dict:
    return {
        'code': error.value,
        'error': _ERROR_MESSAGES.get(error, 'Неизвестная ошибка'),
    }


@router.get('/status')
async def get_modem_status(
    user=Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get modem status and availability for current user."""
    await db.refresh(user, ['subscription'])

    modem_service = get_modem_service()
    availability = modem_service.check_availability(user)

    result = {
        'available': availability.available,
        'modem_enabled': availability.modem_enabled,
        'error_code': availability.error.value if availability.error else None,
        'error_message': _ERROR_MESSAGES.get(availability.error) if availability.error else None,
    }

    subscription = user.subscription
    if subscription and subscription.end_date:
        from datetime import datetime

        remaining_days = max(0, (subscription.end_date - datetime.utcnow()).days)
        result['remaining_days'] = remaining_days
        result['warning_level'] = modem_service.get_period_warning_level(remaining_days)

    return result


@router.get('/price')
async def get_modem_price(
    user=Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Calculate modem price for current user's subscription."""
    await db.refresh(user, ['subscription'])

    modem_service = get_modem_service()

    availability = modem_service.check_availability(user, for_enable=True)
    if not availability.available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_error_detail(availability.error),
        )

    price_info = modem_service.calculate_price(user.subscription)

    return {
        'base_price_kopeks': price_info.base_price,
        'final_price_kopeks': price_info.final_price,
        'discount_percent': price_info.discount_percent,
        'discount_kopeks': price_info.discount_amount,
        'has_discount': price_info.has_discount,
        'charged_months': price_info.charged_months,
        'remaining_days': price_info.remaining_days,
        'end_date': price_info.end_date.isoformat(),
        'base_price_label': settings.format_price(price_info.base_price),
        'final_price_label': settings.format_price(price_info.final_price),
        'balance_kopeks': user.balance_kopeks,
        'balance_sufficient': user.balance_kopeks >= price_info.final_price,
        'missing_amount_kopeks': max(0, price_info.final_price - user.balance_kopeks),
        'missing_amount_label': settings.format_price(max(0, price_info.final_price - user.balance_kopeks)),
        'price_label': settings.format_price(price_info.final_price),
    }


@router.post('/enable')
async def enable_modem(
    user=Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Enable modem for current user's subscription (paid operation)."""
    validation_result = await validate_user_can_purchase(user)
    if not validation_result.can_purchase:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=validation_result.error_message or 'Подключение модема невозможно',
        )

    try:
        await db.refresh(user, ['subscription'])

        modem_service = get_modem_service()

        availability = modem_service.check_availability(user, for_enable=True)
        if not availability.available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_error_detail(availability.error),
            )

        subscription = user.subscription
        price_info = modem_service.calculate_price(subscription)
        price_kopeks = price_info.final_price

        # Check balance
        has_funds, missing = modem_service.check_balance(user, price_kopeks)
        if not has_funds:
            # Save cart for auto-purchase after top-up
            try:
                cart_data = {
                    'cart_mode': 'enable_modem',
                    'price_kopeks': price_kopeks,
                    'base_price_kopeks': price_info.base_price,
                    'discount_percent': price_info.discount_percent,
                    'source': 'cabinet',
                }
                await user_cart_service.save_user_cart(user.id, cart_data)
                logger.info(f'Cart saved for modem enable (cabinet) user {user.id}')
            except Exception as e:
                logger.error(f'Error saving cart for modem enable (cabinet): {e}')

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

        # Enable modem (charge + update + sync)
        result = modem_service.enable_modem(db, user, subscription)
        if hasattr(result, '__await__'):
            result = await result

        if not result.success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=_error_detail(result.error),
            )

        # Send admin notification
        try:
            if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
                bot = Bot(token=settings.BOT_TOKEN)
                try:
                    notification_service = AdminNotificationService(bot)
                    await notification_service.send_subscription_update_notification(
                        db=db,
                        user=user,
                        subscription=subscription,
                        update_type='modem',
                        old_value=False,
                        new_value=True,
                        price_paid=result.charged_amount,
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f'Failed to send admin notification for modem enable: {e}')

        return {
            'success': True,
            'message': 'Модем успешно подключен',
            'charged_kopeks': result.charged_amount,
            'charged_label': settings.format_price(result.charged_amount),
            'new_device_limit': result.new_device_limit,
            'balance_kopeks': user.balance_kopeks,
            'balance_label': settings.format_price(user.balance_kopeks),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Failed to enable modem for user {user.id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Не удалось подключить модем',
        )


@router.post('/disable')
async def disable_modem(
    user=Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Disable modem for current user's subscription (no refund)."""
    try:
        await db.refresh(user, ['subscription'])

        modem_service = get_modem_service()

        availability = modem_service.check_availability(user, for_disable=True)
        if not availability.available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_error_detail(availability.error),
            )

        subscription = user.subscription
        old_device_limit = subscription.device_limit or 1

        result = modem_service.disable_modem(db, user, subscription)
        if hasattr(result, '__await__'):
            result = await result

        if not result.success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=_error_detail(result.error),
            )

        # Send admin notification
        try:
            if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
                bot = Bot(token=settings.BOT_TOKEN)
                try:
                    notification_service = AdminNotificationService(bot)
                    await notification_service.send_subscription_update_notification(
                        db=db,
                        user=user,
                        subscription=subscription,
                        update_type='modem',
                        old_value=True,
                        new_value=False,
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f'Failed to send admin notification for modem disable: {e}')

        return {
            'success': True,
            'message': 'Модем отключен. Возврат средств не производится.',
            'new_device_limit': result.new_device_limit,
            'old_device_limit': old_device_limit,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Failed to disable modem for user {user.id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Не удалось отключить модем',
        )
