"""Tests for `app.cabinet.routes.auth._process_referral_code`'s entry gate.

Background — the bug this closes
---------------------------------
Three Telegram cabinet auth endpoints (`auth_telegram`, `auth_telegram_widget`,
`auth_telegram_oidc`) can resolve `referrer_id` from Redis `pending_referral`
(written by the bot's /start handler) instead of from the request body's
`referral_code`. That resolved id is passed straight into
`create_user(..., referred_by_id=referrer_id)`, so the FK gets set — but the
endpoints then call `_process_referral_code(db, user, request.referral_code,
is_new_user=...)` with the RAW (empty) body field.

The old gate — `if not referral_code or not is_new_user: return` — bailed
out before ever looking at `user.referred_by_id`, so Case 1 (referrer
already set by create_user()) never ran for these Redis-resolved
registrations: no `process_referral_registration()` call, no
`ReferralEarning`, no contest event, no notification. The referral link
silently "worked" (FK set) but paid out nothing.

The fix: the gate must let Case 1 through whenever `user.referred_by_id`
is already set on a new user, regardless of whether `referral_code` (the
raw body field) is empty. Case 2 (resolving a referrer FROM the body's
`referral_code`) must still require a non-empty code — relaxing that arm
would let any caller self-attach a referrer post-hoc (see the security
comment in `auth.py` around the self-referral checks in Case 2 and the
retroactive-attach call sites).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.cabinet.routes.auth import _process_referral_code


def _user(*, user_id: int = 10, referred_by_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, referred_by_id=referred_by_id, email=None)


class _BotCtxMgr:
    async def __aenter__(self) -> AsyncMock:
        return AsyncMock(name='bot')

    async def __aexit__(self, *_a: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _mock_bot(monkeypatch: pytest.MonkeyPatch):
    import app.bot_factory

    monkeypatch.setattr(app.bot_factory, 'create_bot', lambda: _BotCtxMgr(), raising=False)
    yield


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.refresh = AsyncMock(return_value=None)
    return session


@pytest.mark.asyncio
async def test_fires_registration_event_for_new_user_with_preset_referrer_and_no_code(db: AsyncMock) -> None:
    """REGRESSION: new user, referrer already resolved from Redis pending_referral
    (referred_by_id set), but the raw body referral_code is None/empty — the
    event must still fire."""
    user = _user(user_id=10, referred_by_id=20)

    with patch('app.cabinet.routes.auth.process_referral_registration', AsyncMock()) as fire:
        await _process_referral_code(db, user, None, is_new_user=True)

    fire.assert_awaited_once()
    args, kwargs = fire.call_args
    assert args[1] == 10  # new_user_id
    assert args[2] == 20  # referrer_id
    assert kwargs.get('bot') is not None


@pytest.mark.asyncio
async def test_does_not_fire_for_existing_user_even_with_referred_by_id_set(db: AsyncMock) -> None:
    """Existing users (is_new_user=False) never get a referrer assigned or a
    registration event fired here, regardless of referred_by_id."""
    user = _user(user_id=10, referred_by_id=20)

    with patch('app.cabinet.routes.auth.process_referral_registration', AsyncMock()) as fire:
        await _process_referral_code(db, user, None, is_new_user=False)

    fire.assert_not_called()


@pytest.mark.asyncio
async def test_case2_without_code_and_without_preset_referrer_resolves_nothing(db: AsyncMock) -> None:
    """New user, no referrer preset, no referral_code in the body — nothing
    to do; must not touch get_user_by_referral_code or fire the event."""
    user = _user(user_id=10, referred_by_id=None)

    with (
        patch('app.cabinet.routes.auth.get_user_by_referral_code', AsyncMock()) as get_ref,
        patch('app.cabinet.routes.auth.process_referral_registration', AsyncMock()) as fire,
    ):
        await _process_referral_code(db, user, None, is_new_user=True)

    get_ref.assert_not_called()
    fire.assert_not_called()


@pytest.mark.asyncio
async def test_case2_with_explicit_code_still_resolves_and_fires(db: AsyncMock) -> None:
    """Negative-control / no-regression: the original Case 2 path (resolve
    referrer from the request body's referral_code) must keep working."""
    user = _user(user_id=10, referred_by_id=None)
    referrer = SimpleNamespace(id=30, email=None)

    with (
        patch('app.cabinet.routes.auth.get_user_by_referral_code', AsyncMock(return_value=referrer)) as get_ref,
        patch('app.cabinet.routes.auth.process_referral_registration', AsyncMock()) as fire,
    ):
        await _process_referral_code(db, user, 'SOME-CODE', is_new_user=True)

    get_ref.assert_awaited_once_with(db, 'SOME-CODE')
    assert user.referred_by_id == 30
    fire.assert_awaited_once()


@pytest.mark.asyncio
async def test_case2_self_referral_by_id_still_blocked(db: AsyncMock) -> None:
    user = _user(user_id=10, referred_by_id=None)
    self_referrer = SimpleNamespace(id=10, email=None)

    with (
        patch('app.cabinet.routes.auth.get_user_by_referral_code', AsyncMock(return_value=self_referrer)),
        patch('app.cabinet.routes.auth.process_referral_registration', AsyncMock()) as fire,
    ):
        await _process_referral_code(db, user, 'SOME-CODE', is_new_user=True)

    assert user.referred_by_id is None
    fire.assert_not_called()
