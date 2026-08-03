"""Tests for RemnaWave panel version auto-detection (v2 vs v3.1.0).

Covers:
  (a) forced REMNAWAVE_API_VERSION='2' → api_version == 2, no network probe
  (b) forced REMNAWAVE_API_VERSION='3' → api_version == 3, no network probe
  (c) auto + stream user object has 'uuid' key (panel 2.8.x) → v2
  (d) auto + stream user object has 'id' without 'uuid' (panel v3) → v3
  (e) auto + stream endpoint raises RemnaWaveAPIError(404) → v2 + warning logged
  (f) auto + stream ambiguous (empty users / non-JSON / neither key) → secondary
      probe (GET /api/users/by-telegram-id/0); if that is also inconclusive →
      fallback to v2 with an explicit warning telling the admin to set
      REMNAWAVE_API_VERSION explicitly.

Settings is frozen pydantic v2, so we patch the getter method on the *class*
via patch.object(type(settings), 'get_remnawave_api_version', ...) which works
for regular methods (pydantic only freezes field writes, not method slots).

Prod bug (2026-08-03): the old auto-detect treated ANY successful response
from GET /api/users/stream as proof of v3 ("the endpoint only exists in v3").
That premise was wrong — /api/users/stream shipped in RemnaWave 2.8.0 and is
present in every 2.8.x release, so the probe returns 200 there too while the
user schema is still v2-shaped (both `uuid` and `id`). The bot then addressed
users by numeric id against a panel that expected `uuid`, and every write
(e.g. PATCH /api/users to activate a gift) failed with
``{"errors":[{"validation":"uuid","code":"invalid_string","message":"Invalid
uuid","path":["uuid"]}]}``. The fix inspects the *user schema* inside the
stream payload instead of just "did this respond 200": presence of a `uuid`
key on the user object means v2 (2.8.x included), absence of `uuid` with an
`id` key means v3 (v3 dropped `uuid` from the user schema entirely).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError, RemnaWaveTransientError


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


def _mock_logger(module):
    """Patch module.logger with a MagicMock that records warning() calls."""
    warning_calls: list = []
    mock_logger = MagicMock()
    mock_logger.debug = MagicMock()
    mock_logger.info = MagicMock()
    mock_logger.warning = MagicMock(side_effect=lambda *a, **kw: warning_calls.append((a, kw)))
    return mock_logger, warning_calls


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
# (c) THE prod-bug regression test: panel 2.8.x — stream responds 200 and the
# user object has BOTH `uuid` and `id`. Old code treated the mere 200 as proof
# of v3; the correct answer is v2 (uuid-addressing), because v3 dropped `uuid`
# from the schema entirely.
# ---------------------------------------------------------------------------

async def test_auto_detect_v2_when_panel_28x_stream_user_has_uuid_and_id():
    """auto: 2.8.x-shaped stream user (has both uuid and id) must detect as v2."""
    api = _api()
    api._make_request = AsyncMock(
        return_value={
            'response': {
                'users': [
                    {
                        'uuid': '11111111-1111-1111-1111-111111111111',
                        'id': 2287,
                        'username': 'someone',
                    }
                ],
                'hasMore': False,
                'nextCursor': None,
            }
        }
    )

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        version = await api.get_api_version()

    assert version == 2
    # Presence of 'uuid' must be conclusive on the first probe — no need to
    # fall back to the secondary probe.
    api._make_request.assert_called_once()


# ---------------------------------------------------------------------------
# (d) auto + stream user has 'id' without 'uuid' (real v3 schema) → v3
# ---------------------------------------------------------------------------

async def test_auto_detect_v3_when_stream_user_has_id_without_uuid():
    """auto: v3-shaped stream user (id, no uuid) → v3."""
    api = _api()
    api._make_request = AsyncMock(
        return_value={
            'response': {
                'users': [{'id': 42, 'shortUuid': 'short-42', 'username': 'someone'}],
                'hasMore': False,
                'nextCursor': None,
            }
        }
    )

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        version = await api.get_api_version()

    assert version == 3
    api._make_request.assert_called_once()
    call_args = api._make_request.call_args
    assert call_args.args[0] == 'GET'
    assert '/api/users/stream' in call_args.args[1]


# ---------------------------------------------------------------------------
# (d-regression) auto + реальный конверт {'response': {...}} → v3
# Прод-баг 2026-08-02: проба искала ключи в наружном dict ['response'] и
# ошибочно откатывалась в v2, ломая v3-панель (userId=NaN на get_user_by_uuid).
# ---------------------------------------------------------------------------

async def test_auto_detect_v3_unwraps_response_envelope():
    """auto: конверт {'response': {users: [{id}], hasMore}} должен дать v3 (регресс прод-бага)."""
    api = _api()
    api._make_request = AsyncMock(
        return_value={'response': {'users': [{'id': 1}], 'hasMore': False, 'nextCursor': None}}
    )
    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        version = await api.get_api_version()
    assert version == 3


# ---------------------------------------------------------------------------
# (e) auto + stream ambiguous (unexpected shape, no users key at all) →
# secondary probe (by-telegram-id) decides. Route absent (404) → v3.
# ---------------------------------------------------------------------------

async def test_auto_detect_v3_via_secondary_probe_when_stream_shape_unexpected():
    """auto: stream 200 without a usable 'users' list → secondary probe decides.

    Secondary probe (GET /api/users/by-telegram-id/0) 404s → route removed in
    v3 → v3.
    """
    api = _api()
    api._make_request = AsyncMock(
        side_effect=[
            {'response': {'unexpected': 'shape'}},
            RemnaWaveAPIError('Not Found', 404, {}),
        ]
    )
    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        version = await api.get_api_version()
    assert version == 3
    assert api._make_request.call_count == 2


# ---------------------------------------------------------------------------
# (f) auto + empty users list → ambiguous → secondary probe responds 200
# (route still exists) → v2.
# ---------------------------------------------------------------------------

async def test_auto_detect_v2_via_secondary_probe_when_users_list_empty():
    """auto: stream 200 with an empty users list → secondary probe decides.

    Secondary probe (GET /api/users/by-telegram-id/0) succeeds (200) → route
    still exists → v2.
    """
    api = _api()
    api._make_request = AsyncMock(
        side_effect=[
            {'response': {'users': [], 'hasMore': False, 'nextCursor': None}},
            {'response': []},
        ]
    )
    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        version = await api.get_api_version()
    assert version == 2
    assert api._make_request.call_count == 2


# ---------------------------------------------------------------------------
# (g) auto + empty users AND secondary probe inconclusive (non-404 error) →
# final fallback to v2 with an explicit warning.
# ---------------------------------------------------------------------------

async def test_auto_detect_fallback_v2_with_warning_when_secondary_probe_inconclusive():
    """auto: both probes inconclusive → fallback v2 + warning telling admin to set env var."""
    import app.external.remnawave_api as rw_module

    api = _api()
    api._make_request = AsyncMock(
        side_effect=[
            {'response': {'users': [], 'hasMore': False, 'nextCursor': None}},
            RemnaWaveAPIError('Internal Server Error', 500, {}),
        ]
    )

    mock_logger, warning_calls = _mock_logger(rw_module)
    with patch.object(rw_module, 'logger', mock_logger):
        with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
            version = await api.get_api_version()

    assert version == 2
    assert api._make_request.call_count == 2
    assert warning_calls, 'Expected a logger.warning explaining the fallback'
    joined = ' '.join(str(a) for call in warning_calls for a in call[0])
    assert 'REMNAWAVE_API_VERSION' in joined


# ---------------------------------------------------------------------------
# (h) auto + non-JSON body (raw_response) on the primary probe, and a
# transient failure on the secondary probe → fallback to v2 with warning.
# ---------------------------------------------------------------------------

async def test_auto_detect_fallback_v2_with_warning_on_non_json_raw_response():
    """auto: primary probe returns non-JSON body, secondary probe is transient → fallback v2."""
    import app.external.remnawave_api as rw_module

    api = _api()
    api._make_request = AsyncMock(
        side_effect=[
            {'raw_response': '<html>not json</html>'},
            RemnaWaveTransientError('Request timed out'),
        ]
    )

    mock_logger, warning_calls = _mock_logger(rw_module)
    with patch.object(rw_module, 'logger', mock_logger):
        with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
            version = await api.get_api_version()

    assert version == 2
    assert api._make_request.call_count == 2
    assert warning_calls, 'Expected a logger.warning explaining the fallback'
    joined = ' '.join(str(a) for call in warning_calls for a in call[0])
    assert 'REMNAWAVE_API_VERSION' in joined


# ---------------------------------------------------------------------------
# (i) auto + stream raises 404 → v2 + warning logged
# ---------------------------------------------------------------------------

async def test_auto_detect_v2_when_stream_returns_404():
    """auto mode: stream 404 → fall back to v2 and emit a structlog warning."""
    import app.external.remnawave_api as rw_module

    api = _api()
    api._make_request = AsyncMock(
        side_effect=RemnaWaveAPIError('Not Found', 404, {})
    )

    mock_logger, warning_calls = _mock_logger(rw_module)
    with patch.object(rw_module, 'logger', mock_logger):
        with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
            version = await api.get_api_version()

    assert version == 2
    assert warning_calls, 'Expected at least one logger.warning call during version detection'
    api._make_request.assert_called_once()


# ---------------------------------------------------------------------------
# Caching: second call must not re-probe
# ---------------------------------------------------------------------------

async def test_get_api_version_caches_result():
    """Once detected, get_api_version() must return cached value without reprobing."""
    api = _api()
    api._make_request = AsyncMock(
        # v3-shaped user (id, no uuid) so the primary probe alone is conclusive
        # and the secondary probe is never triggered.
        return_value={'response': {'users': [{'id': 7}], 'hasMore': False, 'nextCursor': None}}
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
        return_value={'response': {'users': [{'id': 9}], 'hasMore': False, 'nextCursor': None}}
    )

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        await api.get_api_version()

    assert api.api_version == 3
