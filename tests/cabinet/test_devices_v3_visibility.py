"""Regression tests: cabinet "devices" section empty on RemnaWave v3.

Root cause: ``get_devices``/``delete_device``/``delete_all_devices`` gated the
panel call behind ``_resolve_panel_uuid(subscription, user)`` — a v2-only
helper that reads ``subscription.remnawave_uuid``/``user.remnawave_uuid``.
On v3 the panel no longer hands out a ``uuid`` (only a numeric ``id``, see
``SubscriptionService.create_remnawave_user``: ``updated_user.uuid`` is None
on v3, so ``remnawave_uuid`` is never populated), so the gate always saw
``None`` and short-circuited to an empty list / "User UUID not found" 400
*before* ever attempting the v3-aware ``_panel_ref_for_devices`` resolution
(which knows how to use ``user.remnawave_id``). Users on v3 panels therefore
always saw an empty devices list, even though the panel had their id.

These tests call the route coroutines directly (FastAPI routes are plain
async functions) with mocked dependencies, mirroring the mocking style of
tests/cabinet/test_device_ownership.py and the v2/v3 dual-parametrization of
tests/external/test_remnawave_user_path_dual.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.routes.subscription_modules import devices as devices_module


def _user(*, remnawave_uuid: str | None, remnawave_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        remnawave_uuid=remnawave_uuid,
        remnawave_id=remnawave_id,
        subscriptions=[],
    )


def _subscription(*, remnawave_uuid: str | None, device_limit: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        remnawave_uuid=remnawave_uuid,
        remnawave_short_uuid=None,
        device_limit=device_limit,
        status='active',
        is_trial=False,
    )


def _api_client_cm(api_mock: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=api_mock)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _patch_common(subscription, panel_ref, api_mock):
    """Patch the dependencies every device-route test needs.

    ``panel_ref`` is the (uuid, remna_id) tuple that the v2/v3-aware
    ``get_panel_user_ref`` would resolve — this is what actually encodes
    whether we're simulating a v2 or v3 panel.
    """
    service_mock = MagicMock()
    service_mock.get_api_client = MagicMock(return_value=_api_client_cm(api_mock))

    return [
        patch.object(devices_module, 'resolve_subscription', AsyncMock(return_value=subscription)),
        patch.object(devices_module, 'get_aliases_for_user', AsyncMock(return_value={})),
        patch('app.services.remnawave_service.RemnaWaveService', return_value=service_mock),
        patch('app.services.remnawave_service.get_panel_user_ref', AsyncMock(return_value=panel_ref)),
    ]


# ---------------------------------------------------------------------------
# GET /devices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_devices_v3_returns_devices_by_remna_id() -> None:
    """v3: user has no remnawave_uuid at all, only remnawave_id — must still list devices."""
    user = _user(remnawave_uuid=None, remnawave_id=42)
    subscription = _subscription(remnawave_uuid=None)

    api_mock = MagicMock()
    api_mock.get_user_devices_all = AsyncMock(
        return_value={'devices': [{'hwid': 'HW1', 'platform': 'iOS'}], 'total': 1}
    )

    patches = _patch_common(subscription, panel_ref=(None, 42), api_mock=api_mock)
    with patches[0], patches[1], patches[2], patches[3]:
        result = await devices_module.get_devices(subscription_id=None, user=user, db=AsyncMock())

    assert result['total'] == 1
    assert result['devices'] == [
        {'hwid': 'HW1', 'platform': 'iOS', 'device_model': 'Unknown', 'created_at': None, 'local_name': None}
    ]
    api_mock.get_user_devices_all.assert_awaited_once_with(user_uuid=None, remna_id=42)


@pytest.mark.asyncio
async def test_get_devices_v2_still_uses_uuid() -> None:
    """v2 regression: uuid-based resolution must keep working byte-for-byte."""
    user = _user(remnawave_uuid='legacy-uuid', remnawave_id=None)
    subscription = _subscription(remnawave_uuid='legacy-uuid')

    api_mock = MagicMock()
    api_mock.get_user_devices_all = AsyncMock(return_value={'devices': [], 'total': 0})

    patches = _patch_common(subscription, panel_ref=('legacy-uuid', None), api_mock=api_mock)
    with patches[0], patches[1], patches[2], patches[3]:
        result = await devices_module.get_devices(subscription_id=None, user=user, db=AsyncMock())

    assert result == {'devices': [], 'total': 0, 'device_limit': 5}
    api_mock.get_user_devices_all.assert_awaited_once_with(user_uuid='legacy-uuid', remna_id=None)


@pytest.mark.asyncio
async def test_get_devices_no_identity_degrades_to_empty_without_crash() -> None:
    """Genuinely unresolvable identity (no uuid, no remna_id anywhere) -> empty list, no exception."""
    user = _user(remnawave_uuid=None, remnawave_id=None)
    subscription = _subscription(remnawave_uuid=None)

    api_mock = MagicMock()
    api_mock.get_user_devices_all = AsyncMock(side_effect=AssertionError('must not be called'))

    patches = _patch_common(subscription, panel_ref=(None, None), api_mock=api_mock)
    with patches[0], patches[1], patches[2], patches[3]:
        result = await devices_module.get_devices(subscription_id=None, user=user, db=AsyncMock())

    assert result == {'devices': [], 'total': 0, 'device_limit': 5}
    api_mock.get_user_devices_all.assert_not_awaited()


# ---------------------------------------------------------------------------
# DELETE /devices/{hwid}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_device_v3_resolves_remna_id_instead_of_400() -> None:
    user = _user(remnawave_uuid=None, remnawave_id=42)
    subscription = _subscription(remnawave_uuid=None)

    api_mock = MagicMock()
    api_mock.delete_hwid_device_by_path = AsyncMock(return_value={'response': {'devices': [], 'total': 0}})

    patches = _patch_common(subscription, panel_ref=(None, 42), api_mock=api_mock)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patch.object(devices_module, 'verify_hwid_belongs_to_user', AsyncMock(return_value=True)),
    ):
        result = await devices_module.delete_device(
            hwid='HW1', subscription_id=None, user=user, db=AsyncMock()
        )

    assert result['success'] is True
    api_mock.delete_hwid_device_by_path.assert_awaited_once_with('42', 'HW1')


@pytest.mark.asyncio
async def test_delete_device_v2_regression_uses_uuid_path() -> None:
    user = _user(remnawave_uuid='legacy-uuid', remnawave_id=None)
    subscription = _subscription(remnawave_uuid='legacy-uuid')

    api_mock = MagicMock()
    api_mock.delete_hwid_device_by_path = AsyncMock(return_value={'response': {'devices': [], 'total': 0}})

    patches = _patch_common(subscription, panel_ref=('legacy-uuid', None), api_mock=api_mock)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patch.object(devices_module, 'verify_hwid_belongs_to_user', AsyncMock(return_value=True)),
    ):
        result = await devices_module.delete_device(
            hwid='HW1', subscription_id=None, user=user, db=AsyncMock()
        )

    assert result['success'] is True
    api_mock.delete_hwid_device_by_path.assert_awaited_once_with('legacy-uuid', 'HW1')
