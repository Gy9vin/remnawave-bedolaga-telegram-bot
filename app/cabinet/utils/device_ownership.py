"""Shared helper for verifying that an HWID belongs to a user's panel account.

Used by the cabinet user-facing rename endpoint and the admin rename
endpoint. Extracted into a single module so the two callers can't drift
again — both had `_verify_hwid_belongs_to_user` near-duplicates with
subtly different fallback logic in the past.

Multi-tariff correctness: a single user can own multiple subscriptions
each pointing to a different RemnaWave panel user (different
`remnawave_uuid`s). The previous "take the first non-null uuid"
heuristic produced spurious 404s when the device the user was renaming
was attached to a NON-first subscription. This helper unions device
lists across ALL distinct panel UUIDs the user holds.
"""

from __future__ import annotations

import structlog

from app.database.models import User


logger = structlog.get_logger(__name__)


def _collect_panel_refs(user: User) -> list[tuple[str | None, str | None]]:
    """Return every distinct panel user the user is attached to, as (uuid, short_uuid).

    Includes the legacy single-tariff `user.remnawave_uuid` (short_uuid=None —
    user-level, so v3 uses `user.remnawave_id`) AND each multi-tariff
    subscription's `(remnawave_uuid, remnawave_short_uuid)`. De-duped by uuid
    while preserving insertion order so the most-likely-active user is queried
    first. On v3 an entry may have uuid=None but a short_uuid for resolution.
    """
    seen: dict[str, None] = {}  # ordered set on uuid
    refs: list[tuple[str | None, str | None]] = []
    uuid = getattr(user, 'remnawave_uuid', None)
    if uuid:
        seen[uuid] = None
        refs.append((uuid, None))
    for sub in getattr(user, 'subscriptions', None) or []:
        sub_uuid = getattr(sub, 'remnawave_uuid', None)
        sub_short = getattr(sub, 'remnawave_short_uuid', None)
        if sub_uuid and sub_uuid not in seen:
            seen[sub_uuid] = None
            refs.append((sub_uuid, sub_short))
        elif not sub_uuid and sub_short:
            # v3 без uuid, но с short_uuid — резолвим по нему.
            refs.append((None, sub_short))
    return refs


async def verify_hwid_belongs_to_user(user: User, hwid: str) -> bool:
    """Best-effort check that `hwid` is on one of the user's RemnaWave panels.

    Multi-tariff aware: queries EVERY distinct panel UUID the user owns
    and unions the device sets. Short-circuits on the first match.

    Degrade-open policy: if RemnaWave is unreachable while iterating,
    returns True so renames don't break during transient outages of an
    external dependency. The alias remains user-scoped — there is no
    privacy or authorization concern from accepting a write under
    degraded conditions; at worst we get an orphan alias row.

    Returns False only when we successfully fetched ALL the panel's
    device lists and the hwid appeared in none of them.
    """
    from app.services.remnawave_service import RemnaWaveService

    panel_refs = _collect_panel_refs(user)
    if not panel_refs:
        return False

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            is_v3 = await api.get_api_version() == 3
            user_remna_id = getattr(user, 'remnawave_id', None)
            for panel_uuid, short_uuid in panel_refs:
                # v3: панель идентифицирует юзера числовым id. User-level ref
                # (short_uuid=None) → user.remnawave_id; per-sub → резолвим
                # remna_id по short_uuid. v2: как раньше, по uuid.
                remna_id = None
                if is_v3:
                    if short_uuid is None:
                        remna_id = user_remna_id
                    else:
                        remna_id = await api.resolve_user_id(short_uuid=short_uuid)
                    if remna_id is None and not panel_uuid:
                        continue
                response = await api.get_user_devices_all(user_uuid=panel_uuid, remna_id=remna_id)
                hwids_on_panel = {
                    (d.get('hwid') or d.get('deviceId') or d.get('id')) for d in (response or {}).get('devices', [])
                }
                if hwid in hwids_on_panel:
                    return True
            return False
    except Exception as remnawave_error:
        logger.warning(
            'RemnaWave unreachable during hwid validation, degrading open',
            user_id=getattr(user, 'id', None),
            panel_uuid_count=len(panel_refs),
            error=str(remnawave_error)[:200],
        )
        return True
