"""Tests for RemnaWave panel version auto-detection (v2 vs v3.1.0).

Covers:
  (a) forced REMNAWAVE_API_VERSION='2' → api_version == 2, no network probe
  (b) forced REMNAWAVE_API_VERSION='3' → api_version == 3, no network probe
  (c) auto + stream endpoint returns {users, hasMore, nextCursor} → v3
  (d) auto + stream endpoint raises RemnaWaveAPIError(404) → v2 + warning logged

Settings is frozen pydantic v2, so we patch the getter method on the *class*
via patch.object(type(settings), 'get_remnawave_api_version', ...) which works
for regular methods (pydantic only freezes field writes, not method slots).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


# ---------------------------------------------------------------------------
# (a) Forced version = '2' — no network call
# ---------------------------------------------------------------------------

async def test_forced_version_2_returns_2_without_probe():
    """When REMNAWAVE_API_VERSION='2', detect must return 2 with zero network calls."""
    api = _api()
    api._make_request = AsyncMock()

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='2'):
        version = await api.get_api_version()

    assert version == 2
    api._make_request.assert_not_called()


# ---------------------------------------------------------------------------
# (b) Forced version = '3' — no network call
# ---------------------------------------------------------------------------

async def test_forced_version_3_returns_3_without_probe():
    """When REMNAWAVE_API_VERSION='3', detect must return 3 with zero network calls."""
    api = _api()
    api._make_request = AsyncMock()

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='3'):
        version = await api.get_api_version()

    assert version == 3
    api._make_request.assert_not_called()


# ---------------------------------------------------------------------------
# (c) auto + stream returns v3 response → v3
# ---------------------------------------------------------------------------

async def test_auto_detect_v3_when_stream_returns_users_hasmore():
    """auto mode: stream endpoint returns {users, hasMore, nextCursor} → v3."""
    api = _api()
    api._make_request = AsyncMock(
        # _make_request возвращает конверт {'response': {...}} — как реальная панель.
        return_value={'response': {'users': [], 'hasMore': False, 'nextCursor': None}}
    )

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        version = await api.get_api_version()

    assert version == 3
    api._make_request.assert_called_once()
    call_args = api._make_request.call_args
    assert call_args.args[0] == 'GET'
    assert '/api/users/stream' in call_args.args[1]


# ---------------------------------------------------------------------------
# (c-regression) auto + реальный конверт {'response': {...}} → v3
# Прод-баг 2026-08-02: проба искала ключи в наружном dict ['response'] и
# ошибочно откатывалась в v2, ломая v3-панель (userId=NaN на get_user_by_uuid).
# ---------------------------------------------------------------------------

async def test_auto_detect_v3_unwraps_response_envelope():
    """auto: конверт {'response': {users, hasMore}} должен дать v3 (регресс прод-бага)."""
    api = _api()
    api._make_request = AsyncMock(
        return_value={'response': {'users': [{'id': 1}], 'hasMore': False, 'nextCursor': None}}
    )
    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        version = await api.get_api_version()
    assert version == 3


async def test_auto_detect_v3_on_200_without_expected_keys():
    """auto: успешный 200 без ожидаемых ключей — эндпоинт есть → v3 (не откатываться в v2)."""
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'unexpected': 'shape'}})
    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        version = await api.get_api_version()
    assert version == 3


# ---------------------------------------------------------------------------
# (d) auto + stream raises 404 → v2 + warning logged
# ---------------------------------------------------------------------------

async def test_auto_detect_v2_when_stream_returns_404():
    """auto mode: stream 404 → fall back to v2 and emit a structlog warning."""
    import app.external.remnawave_api as rw_module

    api = _api()
    api._make_request = AsyncMock(
        side_effect=RemnaWaveAPIError('Not Found', 404, {})
    )

    warning_calls: list = []

    real_logger = rw_module.logger

    mock_logger = MagicMock()
    mock_logger.debug = MagicMock()
    mock_logger.info = MagicMock()
    mock_logger.warning = MagicMock(side_effect=lambda *a, **kw: warning_calls.append((a, kw)))

    with patch.object(rw_module, 'logger', mock_logger):
        with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
            version = await api.get_api_version()

    assert version == 2
    assert warning_calls, 'Expected at least one logger.warning call during version detection'


# ---------------------------------------------------------------------------
# Caching: second call must not re-probe
# ---------------------------------------------------------------------------

async def test_get_api_version_caches_result():
    """Once detected, get_api_version() must return cached value without reprobing."""
    api = _api()
    api._make_request = AsyncMock(
        # _make_request возвращает конверт {'response': {...}} — как реальная панель.
        return_value={'response': {'users': [], 'hasMore': False, 'nextCursor': None}}
    )

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        v1 = await api.get_api_version()
        v2 = await api.get_api_version()

    assert v1 == 3
    assert v2 == 3
    # Probe must have been called only once
    assert api._make_request.call_count == 1


# ---------------------------------------------------------------------------
# api_version property raises before detection
# ---------------------------------------------------------------------------

def test_api_version_property_raises_before_detection():
    """Accessing api_version before get_api_version() is called must raise RuntimeError."""
    api = _api()
    with pytest.raises(RuntimeError):
        _ = api.api_version


# ---------------------------------------------------------------------------
# api_version property returns cached value after detection
# ---------------------------------------------------------------------------

async def test_api_version_property_returns_cached_after_detection():
    """After get_api_version() runs, the api_version property returns the cached int."""
    api = _api()
    api._make_request = AsyncMock(
        # _make_request возвращает конверт {'response': {...}} — как реальная панель.
        return_value={'response': {'users': [], 'hasMore': False, 'nextCursor': None}}
    )

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        await api.get_api_version()

    assert api.api_version == 3
