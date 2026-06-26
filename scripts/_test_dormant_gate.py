"""Логический тест гейта по активности для автопродления.

Запускается без реальной БД/Redis: мокаем тяжёлые зависимости через sys.modules.
"""
import sys
import os
import types
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

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
import types as _types
fake_settings = SimpleNamespace(
    AUTOPAY_SKIP_INACTIVE_DAYS=0,
    get_database_url=lambda: 'postgresql+asyncpg://stub/stub',
)
config_stub = _types.ModuleType('app.config')
config_stub.settings = fake_settings
sys.modules['app.config'] = config_stub

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Now import the helper — it does lazy `from app.config import settings as _settings`
from app.utils.user_utils import is_user_dormant_for_autopay  # noqa: E402

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def make_user(last_activity=None, cabinet_last_login=None):
    return SimpleNamespace(last_activity=last_activity, cabinet_last_login=cabinet_last_login)


# Test 1: threshold=0 → always False
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=0)
u = make_user()
assert is_user_dormant_for_autopay(u, NOW) is False, "threshold=0 должен всегда давать False"
print("PASS: threshold=0 → False")

# Test 2: threshold=30, last_activity 5 days ago → False (active)
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
u = make_user(last_activity=NOW - timedelta(days=5))
assert is_user_dormant_for_autopay(u, NOW) is False, "5 дней назад → не спящий"
print("PASS: threshold=30, активность 5 дней назад → False")

# Test 3: threshold=30, last_activity 60 days ago → True (dormant)
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
u = make_user(last_activity=NOW - timedelta(days=60))
assert is_user_dormant_for_autopay(u, NOW) is True, "60 дней назад → спящий"
print("PASS: threshold=30, активность 60 дней назад → True")

# Test 4: both None with threshold=30 → True
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
u = make_user()
assert is_user_dormant_for_autopay(u, NOW) is True, "оба None → спящий"
print("PASS: оба None при threshold=30 → True")

# Test 5: cabinet_last_login recent, last_activity old → False (max wins)
config_stub.settings = SimpleNamespace(AUTOPAY_SKIP_INACTIVE_DAYS=30)
u = make_user(
    last_activity=NOW - timedelta(days=60),
    cabinet_last_login=NOW - timedelta(days=5),
)
assert is_user_dormant_for_autopay(u, NOW) is False, "cabinet свежий → не спящий"
print("PASS: cabinet_last_login свежий, last_activity старый → False (берём max)")

print("\nAll tests PASSED")
