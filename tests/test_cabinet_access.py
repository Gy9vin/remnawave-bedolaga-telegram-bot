"""Тесты доступа к веб-кабинету: хелпер has_cabinet_access + гейт зависимости.

Покрывает:
  1. has_cabinet_access — поведение при разных комбинациях флагов.
  2. get_current_cabinet_user — 403 cabinet_access_denied при отсутствии доступа.
  3. get_current_cabinet_user — пропускает пользователя при cabinet_access=True.
  4. get_current_cabinet_user — пропускает пользователя при CABINET_OPEN_TO_ALL=True.
  5. get_current_cabinet_user — пропускает админа без cabinet_access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status as http_status

from app.config import Settings
from app.database.models import UserStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    *,
    user_id: int = 1,
    telegram_id: int = 100,
    cabinet_access: bool = False,
    email: str | None = None,
    email_verified: bool = False,
    status_value: str = UserStatus.ACTIVE.value,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        username='testuser',
        email=email,
        email_verified=email_verified,
        status=status_value,
        cabinet_access=cabinet_access,
        balance_kopeks=0,
        last_activity=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        cabinet_last_login=None,
        referral_code='ref',
        referred_by_id=None,
        remnawave_uuid=None,
    )


def _make_request() -> MagicMock:
    req = MagicMock()
    req.headers = MagicMock()
    req.headers.get = MagicMock(return_value=None)
    req.method = 'GET'
    req.url = MagicMock()
    req.url.path = '/cabinet/test'
    return req


def _credentials(token: str = 'fake.jwt.token') -> MagicMock:  # noqa: S107 — test sentinel
    return MagicMock(credentials=token)


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)
    return session


# ---------------------------------------------------------------------------
# 1-3. Юнит-тесты хелпера has_cabinet_access
# ---------------------------------------------------------------------------


def test_has_cabinet_access_true_when_user_flag_set() -> None:
    """cabinet_access=True → доступ независимо от глобального флага."""
    from app.utils.cabinet_access import has_cabinet_access

    user = _make_user(cabinet_access=True)
    with patch('app.utils.cabinet_access.settings') as mock_settings:
        mock_settings.CABINET_OPEN_TO_ALL = False
        assert has_cabinet_access(user) is True


def test_has_cabinet_access_true_when_global_flag_set() -> None:
    """CABINET_OPEN_TO_ALL=True → доступ независимо от user.cabinet_access."""
    from app.utils.cabinet_access import has_cabinet_access

    user = _make_user(cabinet_access=False)
    with patch('app.utils.cabinet_access.settings') as mock_settings:
        mock_settings.CABINET_OPEN_TO_ALL = True
        assert has_cabinet_access(user) is True


def test_has_cabinet_access_false_when_both_false() -> None:
    """Оба флага False → доступа нет."""
    from app.utils.cabinet_access import has_cabinet_access

    user = _make_user(cabinet_access=False)
    with patch('app.utils.cabinet_access.settings') as mock_settings:
        mock_settings.CABINET_OPEN_TO_ALL = False
        assert has_cabinet_access(user) is False


# ---------------------------------------------------------------------------
# 4. Гейт: 403 при отсутствии доступа
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_raises_403_when_no_cabinet_access(db: AsyncMock) -> None:
    """Активный пользователь без cabinet_access и без глобального флага → 403."""
    from app.cabinet.dependencies import get_current_cabinet_user

    user = _make_user(cabinet_access=False)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '1', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
        patch('app.cabinet.dependencies.has_cabinet_access', return_value=False),
        # is_admin — метод класса Settings; патчим через patch.object на типе
        patch.object(Settings, 'is_admin', return_value=False),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == http_status.HTTP_403_FORBIDDEN
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail['code'] == 'cabinet_access_denied'


# ---------------------------------------------------------------------------
# 5. Гейт: пропуск при cabinet_access=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_passes_when_user_cabinet_access_true(db: AsyncMock) -> None:
    """cabinet_access=True → пользователь проходит гейт."""
    from app.cabinet.dependencies import get_current_cabinet_user

    user = _make_user(cabinet_access=True)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '1', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
        patch('app.cabinet.dependencies.has_cabinet_access', return_value=True),
        patch('app.cabinet.dependencies.schedule_cabinet_action_log', MagicMock()),
    ):
        result = await get_current_cabinet_user(
            request=_make_request(),
            credentials=_credentials(),
            db=db,
        )

    assert result is user


# ---------------------------------------------------------------------------
# 6. Гейт: пропуск при CABINET_OPEN_TO_ALL=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_passes_when_global_flag_set(db: AsyncMock) -> None:
    """CABINET_OPEN_TO_ALL=True → пользователь без cabinet_access всё равно проходит."""
    from app.cabinet.dependencies import get_current_cabinet_user

    user = _make_user(cabinet_access=False)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '1', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
        patch('app.cabinet.dependencies.has_cabinet_access', return_value=True),
        patch('app.cabinet.dependencies.schedule_cabinet_action_log', MagicMock()),
    ):
        result = await get_current_cabinet_user(
            request=_make_request(),
            credentials=_credentials(),
            db=db,
        )

    assert result is user


# ---------------------------------------------------------------------------
# 7. Гейт: администратор проходит без cabinet_access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_passes_admin_without_cabinet_access(db: AsyncMock) -> None:
    """Администратор без cabinet_access не получает 403 (admin bypass)."""
    from app.cabinet.dependencies import get_current_cabinet_user

    user = _make_user(cabinet_access=False, telegram_id=777)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '1', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
        patch('app.cabinet.dependencies.has_cabinet_access', return_value=False),
        # is_admin — метод класса Settings; патчим через patch.object на типе
        patch.object(Settings, 'is_admin', return_value=True),
        patch('app.cabinet.dependencies.schedule_cabinet_action_log', MagicMock()),
    ):
        result = await get_current_cabinet_user(
            request=_make_request(),
            credentials=_credentials(),
            db=db,
        )

    assert result is user
