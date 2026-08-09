"""restore_from_fallback не должен отправлять в панель дату окончания из прошлого.

Прод-баг: `update-user.command.ts` панели RemnaWave 3.2.0 отвергает PATCH с
`expireAt` в прошлом (`.refine(date > new Date())`). `restore_from_fallback`
брал дату из `subscription.pre_expiry_expire_at` — снимка на момент переезда
в fallback, который для истёкшей подписки ПО ОПРЕДЕЛЕНИЮ в прошлом. Запрос
падал целиком, флаги fallback не снимались, юзер оставался в fallback-сквade
даже после докупки трафика/ручного возврата/вебхука — все три вызывающих
места (`crud/subscription.py`, `admin_expiry_fallback.py`,
`remnawave_webhook_service.py`) не передают `new_expire_at`.

Фикс: источник даты — наша БД (`subscription.end_date`), а не снимок из
прошлого; и в панель никогда не уходит дата не из будущего — вместо неё
`expire_at=None` (клиент такое поле просто не отправляет), сквады и лимит
трафика восстанавливаются как обычно.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import expiry_fallback_service


def _subscription(**overrides) -> SimpleNamespace:
    base = {
        'id': 999,
        'user_id': 42,
        'expiry_fallback_active': True,
        'traffic_fallback_active': False,
        'remnawave_id': 15758,
        'remnawave_short_uuid': None,
        'pre_expiry_squads': ['squad-orig'],
        'pre_expiry_expire_at': None,
        'pre_expiry_traffic_limit_bytes': None,
        'expiry_fallback_started_at': datetime.now(UTC),
        'end_date': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def patch_capture(monkeypatch):
    """Мокает _patch_user_full и перехватывает переданный expire_at."""
    calls = {}

    async def fake_patch_user_full(remna_id_hint, *, squads=None, expire_at=None,
                                    traffic_limit_bytes=None, verify_squad_in=None,
                                    db=None, subscription=None):
        calls['expire_at'] = expire_at
        calls['squads'] = squads
        return True

    monkeypatch.setattr(expiry_fallback_service, '_patch_user_full', fake_patch_user_full)
    return calls


@pytest.mark.asyncio
async def test_past_expire_date_is_not_sent_to_panel_but_squads_are(patch_capture):
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _subscription(pre_expiry_expire_at=past, end_date=past)
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_from_fallback(db, sub, notify=False)

    assert ok is True
    assert patch_capture['expire_at'] is None
    assert patch_capture['squads'] == ['squad-orig']


@pytest.mark.asyncio
async def test_flags_are_cleared_even_when_expire_date_was_in_the_past(patch_capture):
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _subscription(pre_expiry_expire_at=past, end_date=past)
    db = AsyncMock()

    await expiry_fallback_service.restore_from_fallback(db, sub, notify=False)

    assert sub.expiry_fallback_active is False
    assert sub.pre_expiry_expire_at is None
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_future_expire_date_is_sent_as_is(patch_capture):
    future = datetime.now(UTC) + timedelta(days=5)
    sub = _subscription(pre_expiry_expire_at=future, end_date=future)
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_from_fallback(db, sub, notify=False)

    assert ok is True
    assert patch_capture['expire_at'] == future


@pytest.mark.asyncio
async def test_new_expire_at_takes_priority_over_end_date(patch_capture):
    end_date = datetime.now(UTC) - timedelta(days=1)
    new_expire_at = datetime.now(UTC) + timedelta(days=10)
    sub = _subscription(pre_expiry_expire_at=end_date, end_date=end_date)
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_from_fallback(
        db, sub, new_expire_at=new_expire_at, notify=False
    )

    assert ok is True
    assert patch_capture['expire_at'] == new_expire_at


@pytest.mark.asyncio
async def test_end_date_takes_priority_over_pre_expiry_snapshot(patch_capture):
    """Три вызывающих места (докупка трафика, ручной возврат админом, вебхук)
    не передают new_expire_at — им обязана помочь наша end_date, а не
    устаревший снимок pre_expiry_expire_at."""
    stale_snapshot = datetime.now(UTC) - timedelta(days=30)
    current_end_date = datetime.now(UTC) + timedelta(days=7)
    sub = _subscription(pre_expiry_expire_at=stale_snapshot, end_date=current_end_date)
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_from_fallback(db, sub, notify=False)

    assert ok is True
    assert patch_capture['expire_at'] == current_end_date
