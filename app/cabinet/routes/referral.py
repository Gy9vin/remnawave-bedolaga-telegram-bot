"""Referral program routes for cabinet."""

import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import ReferralEarning, User

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.referral import (
    ReferralEarningResponse,
    ReferralEarningsListResponse,
    ReferralInfoResponse,
    ReferralItemResponse,
    ReferralListResponse,
    ReferralTermsResponse,
    WithdrawalBalanceResponse,
    WithdrawalCreateRequest,
    WithdrawalRequestResponse,
    WithdrawalRequestsListResponse,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix='/referral', tags=['Cabinet Referral'])


@router.get('', response_model=ReferralInfoResponse)
async def get_referral_info(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get referral program info for current user."""
    # Get total referrals count
    total_query = select(func.count()).select_from(User).where(User.referred_by_id == user.id)
    total_result = await db.execute(total_query)
    total_referrals = total_result.scalar() or 0

    # Get active referrals (with subscription)
    active_query = (
        select(func.count())
        .select_from(User)
        .where(User.referred_by_id == user.id)
        .where(User.has_had_paid_subscription == True)
    )
    active_result = await db.execute(active_query)
    active_referrals = active_result.scalar() or 0

    # Get total earnings
    earnings_query = select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
        ReferralEarning.user_id == user.id
    )
    earnings_result = await db.execute(earnings_query)
    total_earnings = earnings_result.scalar() or 0

    # Get user's commission percent
    commission_percent = user.referral_commission_percent
    if commission_percent is None:
        commission_percent = settings.REFERRAL_COMMISSION_PERCENT

    # Build referral link
    bot_username = settings.get_bot_username() or 'bot'
    referral_link = f'https://t.me/{bot_username}?start={user.referral_code}'

    return ReferralInfoResponse(
        referral_code=user.referral_code or '',
        referral_link=referral_link,
        total_referrals=total_referrals,
        active_referrals=active_referrals,
        total_earnings_kopeks=total_earnings,
        total_earnings_rubles=total_earnings / 100,
        commission_percent=commission_percent,
    )


@router.get('/list', response_model=ReferralListResponse)
async def get_referral_list(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of invited users."""
    # Base query with eager loading of subscription relationship
    query = select(User).options(selectinload(User.subscription)).where(User.referred_by_id == user.id)

    # Get total count
    count_query = select(func.count()).select_from(User).where(User.referred_by_id == user.id)
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Paginate
    offset = (page - 1) * per_page
    query = query.order_by(desc(User.created_at)).offset(offset).limit(per_page)

    result = await db.execute(query)
    referrals = result.scalars().all()

    items = [
        ReferralItemResponse(
            id=r.id,
            username=r.username,
            first_name=r.first_name,
            created_at=r.created_at,
            has_subscription=r.subscription is not None,
            has_paid=r.has_had_paid_subscription,
        )
        for r in referrals
    ]

    pages = math.ceil(total / per_page) if total > 0 else 1

    return ReferralListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/earnings', response_model=ReferralEarningsListResponse)
async def get_referral_earnings(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get referral earnings history."""
    # Base query
    query = select(ReferralEarning).where(ReferralEarning.user_id == user.id)

    # Get total count and sum
    count_query = select(func.count()).select_from(ReferralEarning).where(ReferralEarning.user_id == user.id)
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    sum_query = select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
        ReferralEarning.user_id == user.id
    )
    sum_result = await db.execute(sum_query)
    total_amount = sum_result.scalar() or 0

    # Paginate
    offset = (page - 1) * per_page
    query = query.order_by(desc(ReferralEarning.created_at)).offset(offset).limit(per_page)

    result = await db.execute(query)
    earnings = result.scalars().all()

    items = []
    for e in earnings:
        # Get referral user info
        referral_query = select(User).where(User.id == e.referral_id)
        referral_result = await db.execute(referral_query)
        referral_user = referral_result.scalar_one_or_none()

        items.append(
            ReferralEarningResponse(
                id=e.id,
                amount_kopeks=e.amount_kopeks,
                amount_rubles=e.amount_kopeks / 100,
                reason=e.reason or 'Referral commission',
                referral_username=referral_user.username if referral_user else None,
                referral_first_name=referral_user.first_name if referral_user else None,
                created_at=e.created_at,
            )
        )

    pages = math.ceil(total / per_page) if total > 0 else 1

    return ReferralEarningsListResponse(
        items=items,
        total=total,
        total_amount_kopeks=total_amount,
        total_amount_rubles=total_amount / 100,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/terms', response_model=ReferralTermsResponse)
async def get_referral_terms():
    """Get referral program terms."""
    return ReferralTermsResponse(
        is_enabled=settings.is_referral_program_enabled(),
        commission_percent=settings.REFERRAL_COMMISSION_PERCENT,
        minimum_topup_kopeks=settings.REFERRAL_MINIMUM_TOPUP_KOPEKS,
        minimum_topup_rubles=settings.REFERRAL_MINIMUM_TOPUP_KOPEKS / 100,
        first_topup_bonus_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
        first_topup_bonus_rubles=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS / 100,
        inviter_bonus_kopeks=settings.REFERRAL_INVITER_BONUS_KOPEKS,
        inviter_bonus_rubles=settings.REFERRAL_INVITER_BONUS_KOPEKS / 100,
    )


@router.get('/withdrawal/balance', response_model=WithdrawalBalanceResponse)
async def get_withdrawal_balance(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get withdrawal balance stats and availability."""
    from app.services.referral_withdrawal_service import referral_withdrawal_service

    if not settings.is_referral_withdrawal_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Функция вывода реферального баланса отключена',
        )

    stats = await referral_withdrawal_service.get_referral_balance_stats(db, user.id)
    can_withdraw, reason = await referral_withdrawal_service.can_request_withdrawal(db, user.id)

    # Генерируем понятное объяснение для пользователя
    explanation = referral_withdrawal_service.build_withdrawal_explanation(stats)

    return WithdrawalBalanceResponse(
        balance_kopeks=stats['actual_balance'],
        total_earned_kopeks=stats['total_earned'],
        referral_spent_kopeks=stats['referral_spent'],
        withdrawn_kopeks=stats['withdrawn'],
        approved_kopeks=stats['approved'],
        pending_kopeks=stats['pending'],
        available_kopeks=stats['available_total'],
        can_withdraw=can_withdraw,
        cannot_withdraw_reason=reason if not can_withdraw else None,
        min_amount_kopeks=settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS,
        cooldown_days=settings.REFERRAL_WITHDRAWAL_COOLDOWN_DAYS,
        only_referral_mode=stats['only_referral_mode'],
        explanation=explanation,
    )


@router.post('/withdrawal/request')
async def create_withdrawal_request(
    request: WithdrawalCreateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a withdrawal request."""
    from app.services.referral_withdrawal_service import referral_withdrawal_service

    if not settings.is_referral_withdrawal_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Функция вывода реферального баланса отключена',
        )

    if request.amount_kopeks <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Сумма должна быть больше 0',
        )

    min_amount = settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS
    if request.amount_kopeks < min_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Минимальная сумма вывода: {min_amount / 100:.0f}₽',
        )

    if not request.payment_details or len(request.payment_details.strip()) < 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Укажите реквизиты для перевода (минимум 10 символов)',
        )

    withdrawal, error = await referral_withdrawal_service.create_withdrawal_request(
        db=db,
        user_id=user.id,
        amount_kopeks=request.amount_kopeks,
        payment_details=request.payment_details.strip(),
    )

    if not withdrawal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    # Send admin notification
    try:
        from aiogram import Bot, types as aiogram_types

        from app.services.admin_notification_service import AdminNotificationService

        if settings.BOT_TOKEN:
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                analysis = await referral_withdrawal_service.analyze_for_money_laundering(db, user.id)
                analysis_text = referral_withdrawal_service.format_analysis_for_admin(analysis)

                user_display = (
                    f'@{user.username}'
                    if user.username
                    else f'{user.first_name or ""} (ID: {user.telegram_id or user.id})'
                )
                user_id_display = user.telegram_id or user.email or f'#{user.id}'
                notification_text = (
                    f'🔔 <b>Новая заявка на вывод #{withdrawal.id}</b>\n\n'
                    f'👤 Пользователь: {user_display}\n'
                    f'🆔 ID: <code>{user_id_display}</code>\n'
                    f'💰 Сумма: <b>{settings.format_price(withdrawal.amount_kopeks)}</b>\n\n'
                    f'💳 Реквизиты:\n'
                    f'<code>{withdrawal.payment_details}</code>\n\n'
                    f'{analysis_text}'
                )

                # Keyboard with approve/reject buttons
                keyboard_rows = [
                    [
                        aiogram_types.InlineKeyboardButton(
                            text='✅ Одобрить',
                            callback_data=f'admin_withdrawal_approve_{withdrawal.id}',
                        ),
                        aiogram_types.InlineKeyboardButton(
                            text='❌ Отклонить',
                            callback_data=f'admin_withdrawal_reject_{withdrawal.id}',
                        ),
                    ]
                ]
                if user.telegram_id:
                    keyboard_rows.append(
                        [
                            aiogram_types.InlineKeyboardButton(
                                text='👤 Профиль пользователя',
                                callback_data=f'admin_user_{user.telegram_id}',
                            )
                        ]
                    )
                admin_keyboard = aiogram_types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

                notification_service = AdminNotificationService(bot)
                withdrawal_topic_id = settings.REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID
                sent = await notification_service.send_withdrawal_request_notification(
                    notification_text, reply_markup=admin_keyboard, topic_id=withdrawal_topic_id
                )
                if not sent:
                    logger.warning(
                        f'Withdrawal notification not sent for request #{withdrawal.id} '
                        f'(chat_id={notification_service.chat_id})'
                    )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error(f'Failed to send admin notification for withdrawal request: {e}')

    return {
        'success': True,
        'message': 'Заявка на вывод создана и ожидает рассмотрения',
        'request_id': withdrawal.id,
        'amount_kopeks': withdrawal.amount_kopeks,
        'status': withdrawal.status,
    }


@router.get('/withdrawal/requests', response_model=WithdrawalRequestsListResponse)
async def get_withdrawal_requests(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user's withdrawal requests history."""
    from app.database.models import WithdrawalRequest

    # Count total
    count_query = select(func.count()).select_from(WithdrawalRequest).where(WithdrawalRequest.user_id == user.id)
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Paginate
    offset = (page - 1) * per_page
    query = (
        select(WithdrawalRequest)
        .where(WithdrawalRequest.user_id == user.id)
        .order_by(desc(WithdrawalRequest.created_at))
        .offset(offset)
        .limit(per_page)
    )

    result = await db.execute(query)
    requests = result.scalars().all()

    items = [
        WithdrawalRequestResponse(
            id=r.id,
            amount_kopeks=r.amount_kopeks,
            status=r.status,
            payment_details=r.payment_details,
            risk_score=r.risk_score or 0,
            admin_comment=r.admin_comment,
            created_at=r.created_at,
            processed_at=r.processed_at,
        )
        for r in requests
    ]

    pages = math.ceil(total / per_page) if total > 0 else 1

    return WithdrawalRequestsListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )
