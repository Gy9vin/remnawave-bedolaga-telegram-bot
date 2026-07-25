"""Regression tests: after merge, survivor holds ALL identities of both accounts.

Spec A4: After merge the survivor must hold ALL identifiers of both:
- telegram_id
- every oauth id (google_id, yandex_id, discord_id, vk_id)
- email + password_hash

And the absorbed account must be status='deleted' with all identifiers NULL.

Two directions tested:
- Telegram account (with active sub) merged INTO email account (survivor = email)
- Email account (with active sub) merged INTO Telegram account (survivor = telegram)

These tests use the SimpleNamespace mock pattern from test_account_merge_service.py.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.services.account_merge_service as _mod
from app.services.account_merge_service import execute_merge


_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_SUB_END = datetime(2026, 12, 31, 0, 0, 0, tzinfo=UTC)


def _make_user(
    id,
    telegram_id=None,
    email=None,
    password_hash=None,
    email_verified=False,
    email_verified_at=None,
    google_id=None,
    yandex_id=None,
    discord_id=None,
    vk_id=None,
    status='active',
    subscriptions=None,
    remnawave_uuid=None,
    balance_kopeks=0,
    referred_by_id=None,
    referral_code=None,
    partner_status='none',
    referral_commission_percent=None,
    has_had_paid_subscription=False,
    has_made_first_topup=False,
    restriction_topup=False,
    restriction_subscription=False,
    restriction_reason=None,
    used_promocodes=0,
):
    subs = subscriptions or []
    return SimpleNamespace(
        id=id,
        telegram_id=telegram_id,
        email=email,
        password_hash=password_hash,
        email_verified=email_verified,
        email_verified_at=email_verified_at,
        email_change_new=None,
        email_change_code=None,
        email_change_expires=None,
        email_verification_token=None,
        email_verification_expires=None,
        password_reset_token=None,
        password_reset_expires=None,
        google_id=google_id,
        yandex_id=yandex_id,
        discord_id=discord_id,
        vk_id=vk_id,
        status=status,
        subscriptions=subs,
        remnawave_uuid=remnawave_uuid,
        balance_kopeks=balance_kopeks,
        referred_by_id=referred_by_id,
        referral_code=referral_code,
        partner_status=partner_status,
        referral_commission_percent=referral_commission_percent,
        has_had_paid_subscription=has_had_paid_subscription,
        has_made_first_topup=has_made_first_topup,
        restriction_topup=restriction_topup,
        restriction_subscription=restriction_subscription,
        restriction_reason=restriction_reason,
        used_promocodes=used_promocodes,
        updated_at=_NOW,
    )


def _make_sub(id, user_id, end_date=None):
    return SimpleNamespace(
        id=id, user_id=user_id,
        end_date=end_date or _SUB_END,
        status='active', is_trial=False,
        autopay_enabled=False, tariff_id=None,
        remnawave_uuid=None,
        tariff=SimpleNamespace(name='Basic'),
        traffic_limit_gb=100.0, traffic_used_gb=0.0, device_limit=3,
    )


def _make_db():
    db = SimpleNamespace(
        execute=AsyncMock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        add=lambda obj: None,
    )
    return db


def _patch_rw_delete():
    return patch.object(_mod, '_delete_remnawave_user_with_fallback', new_callable=AsyncMock)


def _patch_single_tariff():
    # Patch at the class level so Pydantic's frozen model doesn't block attribute writes.
    return patch.object(type(_mod.settings), 'is_multi_tariff_enabled', return_value=False)


def _two_call_mock(user_a, user_b):
    """Returns AsyncMock that yields user_a on first call, user_b on second."""
    return AsyncMock(side_effect=[user_a, user_b])


class TestSingleProfileInvariant:
    async def test_telegram_sub_into_email_survivor_email(self, monkeypatch):
        """
        Scenario: initiator = email account (id=1, no sub), secondary = telegram account (id=2, has sub).
        User picks keep_account = 2 (telegram).
        Role-swap at handler level: survivor_id=2, absorbed_id=1.
        execute_merge is called with primary=2, secondary=1.
        After merge:
          (a) user id=2 (survivor) has telegram_id AND email+password_hash
          (b) user id=2 has the active subscription
          (c) user id=1 (absorbed) is status='deleted', telegram_id=None, email=None
        """
        sub_telegram = _make_sub(id=10, user_id=2)
        telegram_user = _make_user(id=2, telegram_id=99999, subscriptions=[sub_telegram], remnawave_uuid='rw-tg')
        email_user    = _make_user(id=1, email='user@example.com', password_hash='phash', email_verified=True)

        # After role-swap: survivor_id=2 plays primary, absorbed_id=1 plays secondary.
        # execute_merge(db, primary_user_id=2, secondary_user_id=1)
        monkeypatch.setattr(_mod, 'get_user_by_id', _two_call_mock(telegram_user, email_user))
        db = _make_db()
        with _patch_rw_delete(), _patch_single_tariff():
            result = await execute_merge(db, primary_user_id=2, secondary_user_id=1)

        # (a) survivor holds both identities
        assert result.telegram_id == 99999, 'survivor must retain telegram_id'
        assert result.email == 'user@example.com', 'survivor must gain email from absorbed'
        assert result.password_hash == 'phash', 'survivor must gain password_hash from absorbed'

        # (b) subscription stays on survivor
        assert sub_telegram.user_id == 2, 'subscription must remain on survivor (id=2)'

        # (c) absorbed is deleted with all identifiers NULL
        assert email_user.status == 'deleted', 'absorbed must be marked deleted'
        assert email_user.email is None, 'absorbed email must be NULL'
        assert email_user.password_hash is None, 'absorbed password_hash must be NULL'
        assert email_user.telegram_id is None, 'absorbed telegram_id must be NULL (was already None)'

    async def test_email_sub_into_telegram_survivor_telegram(self, monkeypatch):
        """
        Scenario: initiator = telegram account (id=1, no sub), secondary = email account (id=2, has sub).
        User picks keep_account = 2 (email).
        Role-swap: survivor_id=2, absorbed_id=1.
        execute_merge(db, primary_user_id=2, secondary_user_id=1).
        After merge:
          (a) user id=2 (survivor) has email+password_hash AND telegram_id
          (b) user id=2 has the active subscription
          (c) user id=1 (absorbed) status='deleted', telegram_id=None
        """
        sub_email = _make_sub(id=20, user_id=2)
        email_user    = _make_user(id=2, email='user@example.com', password_hash='phash', subscriptions=[sub_email])
        telegram_user = _make_user(id=1, telegram_id=88888)

        monkeypatch.setattr(_mod, 'get_user_by_id', _two_call_mock(email_user, telegram_user))
        db = _make_db()
        with _patch_rw_delete(), _patch_single_tariff():
            result = await execute_merge(db, primary_user_id=2, secondary_user_id=1)

        # (a) survivor holds both identities
        assert result.email == 'user@example.com', 'survivor must retain email'
        assert result.password_hash == 'phash', 'survivor must retain password_hash'
        assert result.telegram_id == 88888, 'survivor must gain telegram_id from absorbed'

        # (b) subscription stays on survivor
        assert sub_email.user_id == 2, 'subscription must remain on survivor (id=2)'

        # (c) absorbed is deleted
        assert telegram_user.status == 'deleted'
        assert telegram_user.telegram_id is None

    async def test_both_have_login_methods_survivor_gets_all(self, monkeypatch):
        """
        Survivor starts with telegram; absorbed has yandex_id + email.
        After merge survivor has telegram + yandex_id + email.
        """
        survivor = _make_user(id=5, telegram_id=77777)
        absorbed = _make_user(id=6, yandex_id='y-123', email='a@b.com', password_hash='hash2')

        monkeypatch.setattr(_mod, 'get_user_by_id', _two_call_mock(survivor, absorbed))
        db = _make_db()
        with _patch_rw_delete(), _patch_single_tariff():
            result = await execute_merge(db, primary_user_id=5, secondary_user_id=6)

        assert result.telegram_id == 77777
        assert result.yandex_id == 'y-123'
        assert result.email == 'a@b.com'
        assert result.password_hash == 'hash2'
        assert absorbed.status == 'deleted'
        assert absorbed.yandex_id is None
        assert absorbed.email is None
