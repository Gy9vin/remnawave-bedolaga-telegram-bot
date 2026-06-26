"""Логический тест гейта по активности для автопродления.

Запускается без реальной БД/Redis: мокаем тяжёлые зависимости через sys.modules.
"""
import sys
import os
import asyncio
import types as _types
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

# --- Stub heavy modules before any app import ---
def _make_stub(*names):
    for name in names:
        sys.modules.setdefault(name, MagicMock())

_make_stub(
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.asyncio',
    'sqlalchemy.future', 'sqlalchemy.dialects', 'sqlalchemy.dialects.postgresql',
    'structlog', 'aioredis', 'redis', 'asyncpg',
)

# Stub app.database.* so import of user_utils doesn't try to connect
db_stub = MagicMock()
for _mod in [
    'app.database', 'app.database.models', 'app.database.database',
    'app.database.crud', 'app.database.crud.user',
]:
    sys.modules[_mod] = db_stub

# Stub app.config with a minimal settings object (real one needs DB URL)
fake_settings = SimpleNamespace(
    AUTOPAY_SKIP_INACTIVE_DAYS=0,
    get_database_url=lambda: 'postgresql+asyncpg://stub/stub',
)
config_stub = _types.ModuleType('app.config')
config_stub.settings = fake_settings
sys.modules['app.config'] = config_stub

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Now import the helper — it does lazy `from app.config import settings as _settings`
from app.utils.user_utils import is_user_dormant_for_autopay, _is_dormant_by_app_activity  # noqa: E402

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def make_user(last_activity=None, cabinet_last_login=None, user_id=42, remnawave_uuid=None):
    return SimpleNamespace(
        id=user_id,
        last_activity=last_activity,
        cabinet_last_login=cabinet_last_login,
        remnawave_uuid=remnawave_uuid,
    )


def make_subscription(remnawave_uuid=None):
    return SimpleNamespace(remnawave_uuid=remnawave_uuid)


def make_remnawave_service(online_at):
    """Build a fake SubscriptionService whose get_api_client yields an api mock."""
    panel_user = SimpleNamespace(online_at=online_at)
    api_mock = AsyncMock()
    api_mock.get_user_by_uuid = AsyncMock(return_value=panel_user)

    svc = MagicMock()

    @asynccontextmanager
    async def _get_api_client():
        yield api_mock

    svc.get_api_client = _get_api_client
    return svc


def make_failing_remnawave_service():
    """Simulate a panel call failure (triggers app-activity fallback)."""
    svc = MagicMock()

    @asynccontextmanager
    async def _get_api_client():
        raise RuntimeError("connection refused")
        yield  # noqa: unreachable — needed so Python treats this as generator

    svc.get_api_client = _get_api_client
    return svc


# ── Sync helper tests (unchanged behaviour) ────────────────────────────────

# Test S1: threshold=0 in _is_dormant_by_app_activity → False
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=0)
u = make_user()
assert _is_dormant_by_app_activity(u, NOW) is False, "threshold=0 → False"
print("PASS [sync]: threshold=0 → False")

# Test S2: threshold=30, activity 5 days ago → False
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
u = make_user(last_activity=NOW - timedelta(days=5))
assert _is_dormant_by_app_activity(u, NOW) is False
print("PASS [sync]: activity 5 days ago → False")

# Test S3: threshold=30, activity 60 days ago → True
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
u = make_user(last_activity=NOW - timedelta(days=60))
assert _is_dormant_by_app_activity(u, NOW) is True
print("PASS [sync]: activity 60 days ago → True")

# Test S4: both None → True
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
u = make_user()
assert _is_dormant_by_app_activity(u, NOW) is True
print("PASS [sync]: both None → True")


# ── Async tests ────────────────────────────────────────────────────────────

async def run_async_tests():
    db = MagicMock()

    # Test A1: threshold=0 → always False (no panel call needed)
    config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=0)
    u = make_user(remnawave_uuid='uuid-1')
    sub = make_subscription(remnawave_uuid='uuid-1')
    svc = make_remnawave_service(online_at=NOW - timedelta(days=5))
    result = await is_user_dormant_for_autopay(db, sub, u, NOW, svc)
    assert result is False, "threshold=0 → False even with stale online_at"
    print("PASS [async]: threshold=0 → False")

    # Test A2: VPN signal — online_at 5 days ago → not dormant
    config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
    u = make_user(remnawave_uuid='uuid-2')
    sub = make_subscription(remnawave_uuid='uuid-2')
    svc = make_remnawave_service(online_at=NOW - timedelta(days=5))
    result = await is_user_dormant_for_autopay(db, sub, u, NOW, svc)
    assert result is False, "online_at 5 days ago → not dormant"
    print("PASS [async]: VPN signal online_at 5 days ago → False")

    # Test A3: VPN signal — online_at 60 days ago → dormant
    config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
    u = make_user(remnawave_uuid='uuid-3')
    sub = make_subscription(remnawave_uuid='uuid-3')
    svc = make_remnawave_service(online_at=NOW - timedelta(days=60))
    result = await is_user_dormant_for_autopay(db, sub, u, NOW, svc)
    assert result is True, "online_at 60 days ago → dormant"
    print("PASS [async]: VPN signal online_at 60 days ago → True")

    # Test A4: panel raises → fallback to app activity (fresh last_activity) → not dormant
    config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
    u = make_user(remnawave_uuid='uuid-4', last_activity=NOW - timedelta(days=5))
    sub = make_subscription(remnawave_uuid='uuid-4')
    svc = make_failing_remnawave_service()
    result = await is_user_dormant_for_autopay(db, sub, u, NOW, svc)
    assert result is False, "panel error + fresh last_activity → not dormant (fallback)"
    print("PASS [async]: panel error + fresh last_activity → False (fallback)")

    # Test A5: panel raises → fallback to app activity (stale last_activity) → dormant
    config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
    u = make_user(remnawave_uuid='uuid-5', last_activity=NOW - timedelta(days=60))
    sub = make_subscription(remnawave_uuid='uuid-5')
    svc = make_failing_remnawave_service()
    result = await is_user_dormant_for_autopay(db, sub, u, NOW, svc)
    assert result is True, "panel error + stale last_activity → dormant (fallback)"
    print("PASS [async]: panel error + stale last_activity → True (fallback)")

    # Test A6: no remnawave_service → pure app-activity fallback (active user)
    config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
    u = make_user(last_activity=NOW - timedelta(days=5))
    sub = make_subscription()
    result = await is_user_dormant_for_autopay(db, sub, u, NOW, remnawave_service=None)
    assert result is False, "no service + fresh activity → not dormant"
    print("PASS [async]: no remnawave_service + fresh last_activity → False")

    # Test A7: no uuid → fallback to app activity (no panel call)
    config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
    u = make_user(remnawave_uuid=None, last_activity=NOW - timedelta(days=60))
    sub = make_subscription(remnawave_uuid=None)
    svc = make_remnawave_service(online_at=NOW - timedelta(days=1))  # would be "active" — but won't be called
    result = await is_user_dormant_for_autopay(db, sub, u, NOW, svc)
    assert result is True, "no uuid → fallback → stale app activity → dormant"
    print("PASS [async]: no remnawave_uuid + stale last_activity → True (fallback)")

    # Test A8: panel returns user with online_at=None → fallback to app activity
    config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
    u = make_user(remnawave_uuid='uuid-8', last_activity=NOW - timedelta(days=5))
    sub = make_subscription(remnawave_uuid='uuid-8')
    svc = make_remnawave_service(online_at=None)  # user never connected
    result = await is_user_dormant_for_autopay(db, sub, u, NOW, svc)
    assert result is False, "online_at=None → fallback → fresh last_activity → not dormant"
    print("PASS [async]: online_at=None from panel → fallback → False")

    print("\nAll async tests PASSED")


asyncio.run(run_async_tests())
print("\nAll tests PASSED")
