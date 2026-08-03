"""Tests for referral preservation when an UNREGISTERED user opens a
``/start webauth_{token}`` deep link.

Background — the bug this closes
---------------------------------
A brand-new user (not yet in the DB) clicks a cabinet referral link
(``?ref=CODE``), chooses the "login via bot" deep-link fallback (the
primary path in RU where oauth.telegram.org is blocked), and lands on
``/start webauth_{token}``. The webauth token carries the referral code
(stashed by ``create_web_auth_token(referral_code=...)``), but the bot
handler's unregistered-user branch (``app/handlers/start.py``) used to
just answer "register first" and ``return`` — the referral code was
never persisted anywhere. The user then runs a bare ``/start``,
registers, and the referrer is lost forever.

The fix peeks the (still-pending, not-yet-consumed) token via
``poll_web_auth_token`` and stashes a Redis ``pending_referral`` entry so
the *next* registration (``create_user`` in
``app/database/crud/user.py``, which already reads
``pending_referral``) attaches the referrer normally.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.start import _save_pending_referral_from_webauth_token


def _referrer(*, user_id: int = 200, telegram_id: int | None = 999) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, telegram_id=telegram_id)


@pytest.mark.asyncio
async def test_saves_pending_referral_when_token_carries_referral_code() -> None:
    db = AsyncMock()
    referrer = _referrer(user_id=200, telegram_id=999)

    with (
        patch(
            'app.handlers.start.poll_web_auth_token',
            AsyncMock(return_value={'status': 'pending', 'referral_code': 'ABCD-EFGH'}),
        ) as poll,
        patch('app.handlers.start.get_user_by_referral_code', AsyncMock(return_value=referrer)) as get_ref,
        patch('app.handlers.start.save_pending_referral', AsyncMock(return_value=True)) as save,
    ):
        await _save_pending_referral_from_webauth_token(db, telegram_id=555, web_auth_token='tok123456789012345')

    poll.assert_awaited_once_with('tok123456789012345')
    get_ref.assert_awaited_once_with(db, 'ABCD-EFGH')
    save.assert_awaited_once_with(555, 'ABCD-EFGH', 200)


@pytest.mark.asyncio
async def test_blocks_self_referral_by_telegram_id() -> None:
    """The clicking user IS the referrer's own Telegram account — must not
    save a pending referral (would let a user farm their own referral bonus
    via a second, not-yet-registered account attempt)."""
    db = AsyncMock()
    referrer = _referrer(user_id=200, telegram_id=555)  # same as the caller's telegram_id

    with (
        patch(
            'app.handlers.start.poll_web_auth_token',
            AsyncMock(return_value={'status': 'pending', 'referral_code': 'ABCD-EFGH'}),
        ),
        patch('app.handlers.start.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.handlers.start.save_pending_referral', AsyncMock()) as save,
    ):
        await _save_pending_referral_from_webauth_token(db, telegram_id=555, web_auth_token='tok123456789012345')

    save.assert_not_called()


@pytest.mark.asyncio
async def test_noop_when_token_has_no_referral_code() -> None:
    db = AsyncMock()

    with (
        patch(
            'app.handlers.start.poll_web_auth_token',
            AsyncMock(return_value={'status': 'pending'}),
        ),
        patch('app.handlers.start.get_user_by_referral_code', AsyncMock()) as get_ref,
        patch('app.handlers.start.save_pending_referral', AsyncMock()) as save,
    ):
        await _save_pending_referral_from_webauth_token(db, telegram_id=555, web_auth_token='tok123456789012345')

    get_ref.assert_not_called()
    save.assert_not_called()


@pytest.mark.asyncio
async def test_noop_when_token_expired_or_missing() -> None:
    db = AsyncMock()

    with (
        patch('app.handlers.start.poll_web_auth_token', AsyncMock(return_value=None)),
        patch('app.handlers.start.get_user_by_referral_code', AsyncMock()) as get_ref,
        patch('app.handlers.start.save_pending_referral', AsyncMock()) as save,
    ):
        await _save_pending_referral_from_webauth_token(db, telegram_id=555, web_auth_token='tok123456789012345')

    get_ref.assert_not_called()
    save.assert_not_called()


@pytest.mark.asyncio
async def test_noop_when_referral_code_not_found() -> None:
    db = AsyncMock()

    with (
        patch(
            'app.handlers.start.poll_web_auth_token',
            AsyncMock(return_value={'status': 'pending', 'referral_code': 'GHOST'}),
        ),
        patch('app.handlers.start.get_user_by_referral_code', AsyncMock(return_value=None)),
        patch('app.handlers.start.save_pending_referral', AsyncMock()) as save,
    ):
        await _save_pending_referral_from_webauth_token(db, telegram_id=555, web_auth_token='tok123456789012345')

    save.assert_not_called()


@pytest.mark.asyncio
async def test_never_raises_when_poll_fails() -> None:
    """Resilience: a Redis hiccup must not crash the /start handler."""
    db = AsyncMock()

    with patch('app.handlers.start.poll_web_auth_token', AsyncMock(side_effect=RuntimeError('redis down'))):
        await _save_pending_referral_from_webauth_token(db, telegram_id=555, web_auth_token='tok123456789012345')


def test_unregistered_branch_calls_the_helper_before_the_registration_prompt() -> None:
    """Source-level pin: `cmd_start`'s unregistered-webauth-user branch must
    call `_save_pending_referral_from_webauth_token` BEFORE (or alongside)
    the "register first" reply, and the reply text itself must stay intact.
    """
    import inspect

    import app.handlers.start as start_module

    source = inspect.getsource(start_module.cmd_start)
    branch_marker = "Web auth attempt from unregistered user"
    branch_pos = source.index(branch_marker)
    reply_pos = source.index('Сначала зарегистрируйтесь в боте', branch_pos)
    helper_pos = source.index('_save_pending_referral_from_webauth_token(', branch_pos)

    assert branch_pos < helper_pos < reply_pos + 200, (
        'the pending-referral helper must be wired into the unregistered-user branch'
    )
