"""Classification rules for scripts.repair_missing_panel_users.

No network, no real database: the panel roster is a plain list of SimpleNamespace
stand-ins for RemnaWaveUser (via _PanelIndex, same helper the identity backfill
tests use), and subscriptions are SimpleNamespace rows carrying only the fields
the classifier reads.
"""

from types import SimpleNamespace

from app.services.remnawave_identity_backfill import _PanelIndex
from scripts.repair_missing_panel_users import classify_subscription


def panel_user(user_id, *, short_uuid=None):
    return SimpleNamespace(
        id=user_id,
        short_uuid=short_uuid,
        username=None,
        telegram_id=None,
        email=None,
    )


def subscription(sub_id=1, *, remnawave_id=None, short_uuid=None):
    return SimpleNamespace(
        id=sub_id,
        remnawave_id=remnawave_id,
        remnawave_short_uuid=short_uuid,
    )


def test_present_when_remnawave_id_resolves_in_roster():
    index = _PanelIndex([panel_user(42, short_uuid='abc')])
    verdict = classify_subscription(subscription(remnawave_id=42), index)
    assert verdict == 'present'


def test_identity_missing_when_id_empty_but_short_uuid_resolves():
    """Link is broken, account is not — this is backfill_remnawave_ids's job."""
    index = _PanelIndex([panel_user(42, short_uuid='abc')])
    verdict = classify_subscription(subscription(remnawave_id=None, short_uuid='abc'), index)
    assert verdict == 'identity_missing'


def test_candidate_when_nothing_resolves():
    index = _PanelIndex([panel_user(1, short_uuid='other')])
    verdict = classify_subscription(subscription(remnawave_id=None, short_uuid=None), index)
    assert verdict == 'candidate'


def test_candidate_when_short_uuid_stored_but_unknown_to_panel():
    """Stored shortUuid the panel no longer knows means the account is gone —
    a create is needed, not a backfill (which would just leave it unresolved)."""
    index = _PanelIndex([panel_user(1, short_uuid='other')])
    verdict = classify_subscription(subscription(remnawave_id=None, short_uuid='deleted'), index)
    assert verdict == 'candidate'


def test_candidate_when_stored_remnawave_id_points_at_deleted_account():
    """remnawave_id is set, but the panel roster no longer has that id — the
    account was deleted out of band. Falling through to shortUuid must not
    happen here because remnawave_id is not empty; a fresh account is needed."""
    index = _PanelIndex([panel_user(99, short_uuid='abc')])
    verdict = classify_subscription(subscription(remnawave_id=42, short_uuid='abc'), index)
    assert verdict == 'candidate'


def test_present_takes_priority_over_identity_missing():
    index = _PanelIndex([panel_user(42, short_uuid='abc')])
    verdict = classify_subscription(subscription(remnawave_id=42, short_uuid='abc'), index)
    assert verdict == 'present'
