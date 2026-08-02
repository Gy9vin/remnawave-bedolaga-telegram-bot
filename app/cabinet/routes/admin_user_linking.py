"""Admin endpoints for manually linking/unlinking email & Telegram to users, and merging accounts."""

import secrets
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import (
    get_user_by_email,
    get_user_by_id,
    get_user_by_telegram_id,
)
from app.database.models import CabinetRefreshToken, User
from app.services.account_merge_service import (
    execute_merge,
    flush_remnawave_deletions,
    _count_active_referrals,
    _get_remnawave_api,
    compute_auth_methods,
)
from app.cabinet.auth.password_utils import hash_password

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/users', tags=['Cabinet Admin User Linking'])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AdminLinkEmailRequest(BaseModel):
    email: EmailStr
    password: str | None = Field(None, min_length=8, max_length=128)


class AdminLinkEmailResponse(BaseModel):
    success: bool
    email: str
    generated_password: str | None = None


class AdminLinkTelegramRequest(BaseModel):
    telegram_id: int = Field(..., gt=0)
    username: str | None = Field(None, max_length=32)
    first_name: str | None = Field(None, max_length=64)


class AdminLinkTelegramResponse(BaseModel):
    success: bool
    telegram_id: int


class AdminUnlinkResponse(BaseModel):
    success: bool


class AdminMergeUsersRequest(BaseModel):
    primary_user_id: int
    secondary_user_id: int
    keep_subscription_id: int | None = None


class AdminMergeUsersResponse(BaseModel):
    success: bool
    primary_user_id: int
    secondary_user_id: int


class AdminMergeDeviceInfo(BaseModel):
    hwid: str | None = None
    app: str | None = None
    platform: str | None = None
    last_seen: str | None = None   # ISO string from panel


class AdminMergeSubPreview(BaseModel):
    subscription_id: int
    tariff_name: str | None
    end_date: datetime | None
    status: str
    subscription_url: str | None
    subscription_crypto_link: str | None
    remnawave_short_uuid: str | None
    devices_count: int | None        # None when panel unavailable
    devices: list[AdminMergeDeviceInfo]


class AdminMergeUserPreview(BaseModel):
    id: int
    username: str | None
    first_name: str | None
    email: str | None
    telegram_id: int | None
    auth_methods: list[str]
    balance_kopeks: int
    referrals_count: int
    created_at: datetime | None
    subscriptions: list[AdminMergeSubPreview]


class AdminMergePreviewResponse(BaseModel):
    primary: AdminMergeUserPreview
    secondary: AdminMergeUserPreview


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _revoke_user_tokens(db: AsyncSession, user_id: int) -> None:
    """Revoke all active refresh tokens for a user."""
    now = datetime.now(UTC)
    await db.execute(
        update(CabinetRefreshToken)
        .where(
            CabinetRefreshToken.user_id == user_id,
            CabinetRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post('/{user_id}/link-email', response_model=AdminLinkEmailResponse)
async def admin_link_email(
    user_id: int,
    request: AdminLinkEmailRequest,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminLinkEmailResponse:
    """Admin: manually link an email address (and optional password) to a user."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    if user.email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='User already has email linked. Use unlink first.',
        )

    normalized_email = request.email.strip().lower()

    existing = await get_user_by_email(db, normalized_email)
    if existing and existing.id != user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Email already belongs to another account. Use merge to combine accounts.',
        )

    generated_password: str | None = None
    if request.password:
        pw_hash = hash_password(request.password)
    else:
        generated_password = secrets.token_urlsafe(12)
        pw_hash = hash_password(generated_password)

    user.email = normalized_email
    user.password_hash = pw_hash
    user.email_verified = False
    user.updated_at = datetime.now(UTC)

    await db.commit()

    logger.info(
        'Admin linked email to user',
        admin_id=admin.id,
        target_user_id=user.id,
        email=user.email,
        password_generated=generated_password is not None,
    )

    try:
        from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

        _admin_id = admin.id
        _target_id = user.id
        _email = user.email

        async def _notify(svc, bg_db):
            await svc.send_admin_notification(
                f'🔗 Admin #{_admin_id} linked email <code>{_email}</code> to user #{_target_id}'
            )

        dispatch_generic_admin_notification_bg(_notify)
    except Exception:
        pass

    return AdminLinkEmailResponse(
        success=True,
        email=user.email,
        generated_password=generated_password,
    )


@router.post('/{user_id}/link-telegram', response_model=AdminLinkTelegramResponse)
async def admin_link_telegram(
    user_id: int,
    request: AdminLinkTelegramRequest,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminLinkTelegramResponse:
    """Admin: manually link a Telegram ID to a user."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    if user.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='User already has Telegram linked. Use unlink first.',
        )

    existing = await get_user_by_telegram_id(db, request.telegram_id)
    if existing and existing.id != user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Telegram ID already belongs to another account. Use merge to combine accounts.',
        )

    user.telegram_id = request.telegram_id
    if request.username and not user.username:
        user.username = request.username
    if request.first_name and not user.first_name:
        user.first_name = request.first_name
    user.updated_at = datetime.now(UTC)

    await db.commit()

    logger.info(
        'Admin linked Telegram to user',
        admin_id=admin.id,
        target_user_id=user.id,
        telegram_id=request.telegram_id,
    )

    try:
        from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

        _admin_id = admin.id
        _target_id = user.id
        _tg_id = request.telegram_id

        async def _notify(svc, bg_db):
            await svc.send_admin_notification(
                f'🔗 Admin #{_admin_id} linked Telegram ID <code>{_tg_id}</code> to user #{_target_id}'
            )

        dispatch_generic_admin_notification_bg(_notify)
    except Exception:
        pass

    return AdminLinkTelegramResponse(success=True, telegram_id=request.telegram_id)


@router.delete('/{user_id}/link-email', response_model=AdminUnlinkResponse)
async def admin_unlink_email(
    user_id: int,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminUnlinkResponse:
    """Admin: unlink email/password from a user. Refuses if it is the last auth method."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No email to unlink',
        )

    # Prevent removing the last auth method
    if not user.telegram_id:
        # Check OAuth columns too
        from app.database.crud.user import OAUTH_PROVIDER_COLUMNS

        has_oauth = any(getattr(user, col, None) for col in OAUTH_PROVIDER_COLUMNS.values())
        if not has_oauth:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Cannot unlink last authentication method. Link Telegram or another provider first.',
            )

    old_email = user.email
    user.email = None
    user.password_hash = None
    user.email_verified = False
    user.email_verified_at = None
    user.email_verification_token = None
    user.email_verification_expires = None
    user.email_change_new = None
    user.email_change_code = None
    user.email_change_expires = None
    user.updated_at = datetime.now(UTC)

    await _revoke_user_tokens(db, user_id)
    await db.commit()

    logger.info(
        'Admin unlinked email from user',
        admin_id=admin.id,
        target_user_id=user_id,
        email=old_email,
    )

    try:
        from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

        _admin_id = admin.id
        _target_id = user_id
        _email = old_email

        async def _notify(svc, bg_db):
            await svc.send_admin_notification(
                f'🔓 Admin #{_admin_id} unlinked email <code>{_email}</code> from user #{_target_id}'
            )

        dispatch_generic_admin_notification_bg(_notify)
    except Exception:
        pass

    return AdminUnlinkResponse(success=True)


@router.delete('/{user_id}/link-telegram', response_model=AdminUnlinkResponse)
async def admin_unlink_telegram(
    user_id: int,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminUnlinkResponse:
    """Admin: unlink Telegram from a user. Refuses if it is the last auth method."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    if not user.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No Telegram to unlink',
        )

    # Prevent removing the last auth method
    if not user.email:
        from app.database.crud.user import OAUTH_PROVIDER_COLUMNS

        has_oauth = any(getattr(user, col, None) for col in OAUTH_PROVIDER_COLUMNS.values())
        if not has_oauth:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Cannot unlink last authentication method. Link email or another provider first.',
            )

    old_tg_id = user.telegram_id
    user.telegram_id = None
    user.updated_at = datetime.now(UTC)

    await _revoke_user_tokens(db, user_id)
    await db.commit()

    logger.info(
        'Admin unlinked Telegram from user',
        admin_id=admin.id,
        target_user_id=user_id,
        telegram_id=old_tg_id,
    )

    try:
        from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

        _admin_id = admin.id
        _target_id = user_id
        _tg_id = old_tg_id

        async def _notify(svc, bg_db):
            await svc.send_admin_notification(
                f'🔓 Admin #{_admin_id} unlinked Telegram ID <code>{_tg_id}</code> from user #{_target_id}'
            )

        dispatch_generic_admin_notification_bg(_notify)
    except Exception:
        pass

    return AdminUnlinkResponse(success=True)


@router.get('/merge/preview', response_model=AdminMergePreviewResponse)
async def admin_merge_preview(
    primary_user_id: int = Query(...),
    secondary_user_id: int = Query(...),
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminMergePreviewResponse:
    """Preview merge: return both users' base info + subscriptions with live device counts."""
    if primary_user_id == secondary_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='primary_user_id and secondary_user_id must be different',
        )

    primary = await get_user_by_id(db, primary_user_id)
    if not primary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Primary user (id={primary_user_id}) not found',
        )
    secondary = await get_user_by_id(db, secondary_user_id)
    if not secondary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Secondary user (id={secondary_user_id}) not found',
        )

    primary_refs = await _count_active_referrals(db, primary_user_id)
    secondary_refs = await _count_active_referrals(db, secondary_user_id)

    async def _build_sub_previews(user: User) -> list[AdminMergeSubPreview]:
        subs = getattr(user, 'subscriptions', None) or []
        previews: list[AdminMergeSubPreview] = []
        for sub in subs:
            remnawave_uuid = getattr(sub, 'remnawave_uuid', None)
            devices_count: int | None = None
            devices: list[AdminMergeDeviceInfo] = []
            if remnawave_uuid or getattr(user, 'remnawave_id', None):
                try:
                    from app.services.remnawave_service import get_panel_user_ref

                    async with _get_remnawave_api() as api:
                        _p_uuid, _p_id = await get_panel_user_ref(api, db, user=user, subscription=sub)
                        data = await api.get_user_devices_all(
                            user_uuid=_p_uuid or remnawave_uuid, remna_id=_p_id
                        )
                    raw_devices = data.get('devices', [])
                    devices_count = data.get('total', len(raw_devices))
                    for d in raw_devices:
                        devices.append(AdminMergeDeviceInfo(
                            hwid=d.get('hwid'),
                            app=d.get('userAgent') or d.get('app') or d.get('appName'),
                            platform=d.get('platform'),
                            last_seen=d.get('lastSeen') or d.get('last_seen'),
                        ))
                except Exception:
                    logger.warning(
                        'Failed to fetch devices for subscription in merge preview',
                        subscription_id=sub.id,
                        remnawave_uuid=remnawave_uuid,
                        exc_info=True,
                    )
                    # devices_count stays None, devices stays []
            tariff_name = None
            if getattr(sub, 'tariff', None):
                tariff_name = sub.tariff.name
            previews.append(AdminMergeSubPreview(
                subscription_id=sub.id,
                tariff_name=tariff_name,
                end_date=getattr(sub, 'end_date', None),
                status=sub.status,
                subscription_url=getattr(sub, 'subscription_url', None),
                subscription_crypto_link=getattr(sub, 'subscription_crypto_link', None),
                remnawave_short_uuid=getattr(sub, 'remnawave_short_uuid', None),
                devices_count=devices_count,
                devices=devices,
            ))
        return previews

    primary_subs = await _build_sub_previews(primary)
    secondary_subs = await _build_sub_previews(secondary)

    def _build_user_preview(user: User, subs: list[AdminMergeSubPreview], refs: int) -> AdminMergeUserPreview:
        return AdminMergeUserPreview(
            id=user.id,
            username=getattr(user, 'username', None),
            first_name=getattr(user, 'first_name', None),
            email=getattr(user, 'email', None),
            telegram_id=getattr(user, 'telegram_id', None),
            auth_methods=compute_auth_methods(user),
            balance_kopeks=getattr(user, 'balance_kopeks', 0),
            referrals_count=refs,
            created_at=getattr(user, 'created_at', None),
            subscriptions=subs,
        )

    return AdminMergePreviewResponse(
        primary=_build_user_preview(primary, primary_subs, primary_refs),
        secondary=_build_user_preview(secondary, secondary_subs, secondary_refs),
    )


@router.post('/merge', response_model=AdminMergeUsersResponse)
async def admin_merge_users(
    request: AdminMergeUsersRequest,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminMergeUsersResponse:
    """Admin: merge two accounts. Primary stays, secondary is absorbed and deleted.

    Transfers balance, subscriptions, transactions, referrals and all related data.
    Always keeps the primary user's subscription when both have one.
    """
    if request.primary_user_id == request.secondary_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='primary_user_id and secondary_user_id must be different',
        )

    # Verify both users exist before executing merge
    primary = await get_user_by_id(db, request.primary_user_id)
    if not primary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Primary user (id={request.primary_user_id}) not found',
        )
    secondary = await get_user_by_id(db, request.secondary_user_id)
    if not secondary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Secondary user (id={request.secondary_user_id}) not found',
        )

    # Validate keep_subscription_id belongs to one of the two users
    if request.keep_subscription_id is not None:
        all_sub_ids: set[int] = set()
        for sub in (getattr(primary, 'subscriptions', None) or []):
            all_sub_ids.add(sub.id)
        for sub in (getattr(secondary, 'subscriptions', None) or []):
            all_sub_ids.add(sub.id)
        if request.keep_subscription_id not in all_sub_ids:
            logger.warning(
                'keep_subscription_id не принадлежит ни одному из объединяемых пользователей',
                keep_subscription_id=request.keep_subscription_id,
                valid_sub_ids=sorted(all_sub_ids),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f'keep_subscription_id={request.keep_subscription_id} does not belong '
                    f'to either merged user'
                ),
            )

    deferred_deletions: list = []  # (uuid, short_uuid[, remna_id])-кортежи или строки uuid
    try:
        await execute_merge(
            db=db,
            primary_user_id=request.primary_user_id,
            secondary_user_id=request.secondary_user_id,
            provider='admin_manual',
            provider_id=str(admin.id),
            deferred_remnawave_deletions=deferred_deletions,
            keep_subscription_id=request.keep_subscription_id,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception(
            'Admin merge failed',
            admin_id=admin.id,
            primary_user_id=request.primary_user_id,
            secondary_user_id=request.secondary_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Account merge failed due to an internal error',
        ) from exc

    # External RemnaWave deletions after commit
    await flush_remnawave_deletions(deferred_deletions)

    logger.info(
        'Admin merged accounts',
        admin_id=admin.id,
        primary_user_id=request.primary_user_id,
        secondary_user_id=request.secondary_user_id,
    )

    try:
        from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

        _admin_id = admin.id
        _primary_id = request.primary_user_id
        _secondary_id = request.secondary_user_id

        async def _notify(svc, bg_db):
            await svc.send_admin_notification(
                f'🔀 Admin #{_admin_id} merged accounts: '
                f'secondary #{_secondary_id} absorbed into primary #{_primary_id}'
            )

        dispatch_generic_admin_notification_bg(_notify)
    except Exception:
        pass

    return AdminMergeUsersResponse(
        success=True,
        primary_user_id=request.primary_user_id,
        secondary_user_id=request.secondary_user_id,
    )
