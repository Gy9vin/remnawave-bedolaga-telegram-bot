"""Admin endpoints for manually linking/unlinking email & Telegram to users, and merging accounts."""

import secrets
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
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


class AdminMergeUsersResponse(BaseModel):
    success: bool
    primary_user_id: int
    secondary_user_id: int


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

    deferred_deletions: list[str] = []
    try:
        await execute_merge(
            db=db,
            primary_user_id=request.primary_user_id,
            secondary_user_id=request.secondary_user_id,
            keep_subscription_from='primary',
            provider='admin_manual',
            provider_id=str(admin.id),
            deferred_remnawave_deletions=deferred_deletions,
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
