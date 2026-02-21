"""OAuth 2.0 authentication routes for cabinet."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import (
    create_user_by_oauth,
    get_user_by_email,
    get_user_by_oauth_provider,
    get_user_by_referral_code,
    set_user_oauth_provider_id,
)
from app.database.models import User

from ..auth.oauth_providers import (
    OAuthUserInfo,
    generate_oauth_state,
    get_provider,
    validate_oauth_state,
)
from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.auth import (
    AuthResponse,
    ConnectionInfo,
    ConnectionsResponse,
    LinkOAuthRequest,
    LinkResponse,
)
from .auth import _create_auth_response, _process_campaign_bonus, _store_refresh_token


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/auth/oauth', tags=['Cabinet OAuth'])


async def _finalize_oauth_login(
    db: AsyncSession,
    user: User,
    provider: str,
    campaign_slug: str | None = None,
    referral_code: str | None = None,
) -> AuthResponse:
    """Update last login, create tokens, store refresh token."""
    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()
    auth_response = _create_auth_response(user)
    await _store_refresh_token(db, user.id, auth_response.refresh_token, device_info=f'oauth:{provider}')

    # Process referral code (before campaign bonus, which may also set referrer)
    from .auth import _process_referral_code, _user_to_response

    await _process_referral_code(db, user, referral_code)

    auth_response.campaign_bonus = await _process_campaign_bonus(db, user, campaign_slug)
    if auth_response.campaign_bonus:
        auth_response.user = _user_to_response(user)
    return auth_response


# --- Schemas ---


class OAuthProviderInfo(BaseModel):
    name: str
    display_name: str


class OAuthProvidersResponse(BaseModel):
    providers: list[OAuthProviderInfo]


class OAuthAuthorizeResponse(BaseModel):
    authorize_url: str
    state: str


class OAuthCallbackRequest(BaseModel):
    code: str = Field(..., description='Authorization code from provider')
    state: str = Field(..., description='CSRF state token')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(None, max_length=32, description='Referral code of inviter')


# --- Endpoints ---


@router.get('/providers', response_model=OAuthProvidersResponse)
async def get_oauth_providers():
    """Get list of enabled OAuth providers."""
    providers_config = settings.get_oauth_providers_config()
    providers = [
        OAuthProviderInfo(name=name, display_name=cfg['display_name'])
        for name, cfg in providers_config.items()
        if cfg['enabled']
    ]
    return OAuthProvidersResponse(providers=providers)


@router.get('/{provider}/authorize', response_model=OAuthAuthorizeResponse)
async def get_oauth_authorize_url(provider: str):
    """Get authorization URL for an OAuth provider."""
    oauth_provider = get_provider(provider)
    if not oauth_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'OAuth provider "{provider}" is not enabled',
        )

    state = await generate_oauth_state(provider)
    authorize_url = oauth_provider.get_authorization_url(state)

    return OAuthAuthorizeResponse(authorize_url=authorize_url, state=state)


@router.post('/{provider}/callback', response_model=AuthResponse)
async def oauth_callback(
    provider: str,
    request: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Handle OAuth callback: exchange code, find/create user, return JWT."""
    # 1. Validate CSRF state
    if not await validate_oauth_state(request.state, provider):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid or expired OAuth state',
        )

    # 2. Get provider instance
    oauth_provider = get_provider(provider)
    if not oauth_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'OAuth provider "{provider}" is not enabled',
        )

    # 3. Exchange code for tokens
    try:
        token_data = await oauth_provider.exchange_code(request.code)
    except Exception as exc:
        logger.error('OAuth code exchange failed for', provider=provider, exc=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to exchange authorization code',
        ) from exc

    # 4. Fetch user info from provider
    try:
        user_info: OAuthUserInfo = await oauth_provider.get_user_info(token_data)
    except Exception as exc:
        logger.error('OAuth user info fetch failed for', provider=provider, exc=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to fetch user information from provider',
        ) from exc

    # 5. Find user by provider ID
    user = await get_user_by_oauth_provider(db, provider, user_info.provider_id)
    if user:
        logger.info('OAuth login via for existing user', provider=provider, user_id=user.id)
        return await _finalize_oauth_login(db, user, provider, request.campaign_slug, request.referral_code)

    # 6. Find user by email (if verified) and link provider
    if user_info.email and user_info.email_verified:
        user = await get_user_by_email(db, user_info.email)
        if user:
            await set_user_oauth_provider_id(db, user, provider, user_info.provider_id)
            logger.info('OAuth login via linked to existing email user', provider=provider, user_id=user.id)
            return await _finalize_oauth_login(db, user, provider, request.campaign_slug, request.referral_code)

    # 7. Resolve referral code for new user
    referrer_id = None
    if request.referral_code:
        try:
            referrer = await get_user_by_referral_code(db, request.referral_code)
            if referrer:
                # Self-referral protection by email
                if (
                    user_info.email
                    and user_info.email_verified
                    and referrer.email
                    and referrer.email.lower() == user_info.email.lower()
                ):
                    logger.warning(
                        'Self-referral attempt blocked via OAuth',
                        referral_code=request.referral_code,
                        email=user_info.email,
                    )
                else:
                    referrer_id = referrer.id
        except Exception as e:
            logger.warning('Failed to resolve referral code during OAuth', referral_code=request.referral_code, error=e)

    # 8. Create new user
    user = await create_user_by_oauth(
        db=db,
        provider=provider,
        provider_id=user_info.provider_id,
        email=user_info.email if user_info.email_verified else None,
        email_verified=user_info.email_verified,
        first_name=user_info.first_name,
        last_name=user_info.last_name,
        username=user_info.username,
        referred_by_id=referrer_id,
    )
    logger.info('OAuth new user created via with id', provider=provider, user_id=user.id)
    return await _finalize_oauth_login(db, user, provider, request.campaign_slug, request.referral_code)


# --- Account Linking Endpoints ---

_PROVIDER_COLUMNS = {
    'google': 'google_id',
    'yandex': 'yandex_id',
    'discord': 'discord_id',
    'vk': 'vk_id',
}


def _count_auth_methods(user: User) -> int:
    """Count how many auth methods a user has (to prevent unlinking the last one)."""
    count = 0
    if user.telegram_id:
        count += 1
    if user.email and user.email_verified and user.password_hash:
        count += 1
    for col in _PROVIDER_COLUMNS.values():
        if getattr(user, col, None):
            count += 1
    return count


@router.get('/connections', response_model=ConnectionsResponse)
async def get_connections(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get all connected accounts for current user."""
    connections = []

    # Telegram
    connections.append(
        ConnectionInfo(
            provider='telegram',
            connected=user.telegram_id is not None,
            identifier=f'@{user.username}' if user.username else (str(user.telegram_id) if user.telegram_id else None),
        )
    )

    # Email
    connections.append(
        ConnectionInfo(
            provider='email',
            connected=bool(user.email and user.email_verified),
            identifier=user.email if user.email and user.email_verified else None,
        )
    )

    # OAuth providers
    providers_config = settings.get_oauth_providers_config()
    for provider_name, col_name in _PROVIDER_COLUMNS.items():
        cfg = providers_config.get(provider_name, {})
        if not cfg.get('enabled'):
            continue
        provider_id = getattr(user, col_name, None)
        connections.append(
            ConnectionInfo(
                provider=provider_name,
                connected=provider_id is not None,
                identifier=str(provider_id) if provider_id else None,
            )
        )

    total = sum(1 for c in connections if c.connected)
    return ConnectionsResponse(connections=connections, total_connected=total)


@router.get('/{provider}/link', response_model=OAuthAuthorizeResponse)
async def get_link_authorize_url(
    provider: str,
    user: User = Depends(get_current_cabinet_user),
):
    """Get authorization URL to link an OAuth provider to current account."""
    if provider not in _PROVIDER_COLUMNS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Unknown provider: {provider}')

    # Check if already linked
    if getattr(user, _PROVIDER_COLUMNS[provider], None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'{provider} already linked')

    oauth_provider = get_provider(provider)
    if not oauth_provider:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Provider {provider} is not enabled')

    state = await generate_oauth_state(provider)
    authorize_url = oauth_provider.get_authorization_url(state)
    return OAuthAuthorizeResponse(authorize_url=authorize_url, state=state)


@router.post('/{provider}/link', response_model=LinkResponse)
async def link_oauth_provider(
    provider: str,
    request: LinkOAuthRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Link an OAuth provider to current account."""
    if provider not in _PROVIDER_COLUMNS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Unknown provider: {provider}')

    # Check if already linked
    col_name = _PROVIDER_COLUMNS[provider]
    if getattr(user, col_name, None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'{provider} already linked')

    # Validate state
    if not await validate_oauth_state(request.state, provider):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid or expired state')

    # Get provider and exchange code
    oauth_provider = get_provider(provider)
    if not oauth_provider:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Provider {provider} is not enabled')

    try:
        token_data = await oauth_provider.exchange_code(request.code)
    except Exception as exc:
        logger.error('OAuth link code exchange failed', provider=provider, exc=exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Failed to exchange code') from exc

    try:
        user_info: OAuthUserInfo = await oauth_provider.get_user_info(token_data)
    except Exception as exc:
        logger.error('OAuth link user info failed', provider=provider, exc=exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Failed to get user info') from exc

    # Check that this provider_id is not linked to another user
    existing = await get_user_by_oauth_provider(db, provider, user_info.provider_id)
    if existing and existing.id != user.id:
        # If the other user is "empty" (no telegram, no subscription, no balance),
        # auto-transfer the provider_id to the current user
        is_empty = not existing.telegram_id and not existing.remnawave_uuid and (existing.balance_kopeks or 0) == 0
        if is_empty:
            logger.info(
                'Auto-transferring provider from empty user',
                provider=provider,
                from_user_id=existing.id,
                to_user_id=user.id,
            )
            # Remove provider_id from the empty user
            setattr(existing, col_name, None)
            existing.updated_at = datetime.now(UTC)
            # Flush to DB BEFORE setting new value — otherwise SQLAlchemy batches
            # both UPDATEs into one executemany and the unique constraint fails
            await db.flush()
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'This {provider} account is already linked to another user',
            )

    # Link provider
    await set_user_oauth_provider_id(db, user, provider, user_info.provider_id)
    await db.commit()

    logger.info('OAuth provider linked', provider=provider, user_id=user.id, provider_id=user_info.provider_id)
    return LinkResponse(success=True, message=f'{provider} linked successfully', provider=provider)


@router.delete('/{provider}/link', response_model=LinkResponse)
async def unlink_oauth_provider(
    provider: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Unlink an OAuth provider from current account."""
    if provider not in _PROVIDER_COLUMNS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Unknown provider: {provider}')

    col_name = _PROVIDER_COLUMNS[provider]
    if not getattr(user, col_name, None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'{provider} is not linked')

    # Ensure at least one other auth method remains
    if _count_auth_methods(user) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Cannot unlink the last authentication method',
        )

    setattr(user, col_name, None)
    user.updated_at = datetime.now(UTC)
    await db.commit()

    logger.info('OAuth provider unlinked', provider=provider, user_id=user.id)
    return LinkResponse(success=True, message=f'{provider} unlinked successfully', provider=provider)
