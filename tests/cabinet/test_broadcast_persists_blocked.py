"""Unit test: _send_batched returns blocked_telegram_ids as 5th element.

Strategy: directly call BroadcastService._send_batched with a patched _deliver_message
that raises TelegramForbiddenError for specific telegram_ids, and assert that the 5th
element of the return tuple matches the expected set of blocked ids.

This does NOT require a real DB or bot — only the in-process async send logic is exercised.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError

from app.services.broadcast_service import BroadcastConfig, BroadcastService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config() -> BroadcastConfig:
    return BroadcastConfig(
        target='all',
        message_text='hello',
        selected_buttons=[],
        media=None,
        initiator_name='test',
        custom_buttons=None,
        category='system',
    )


def _make_forbidden_error() -> TelegramForbiddenError:
    """Construct a minimal TelegramForbiddenError without a real HTTP response."""
    err = TelegramForbiddenError.__new__(TelegramForbiddenError)
    err.message = 'Forbidden: bot was blocked by the user'
    err.method = None
    return err


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_batched_returns_blocked_ids(monkeypatch):
    """_send_batched must include blocked telegram_ids in position [4] of the return tuple."""
    BLOCKED_IDS = {1001, 1003}
    ALL_IDS = [1001, 1002, 1003, 1004]

    service = BroadcastService()

    forbidden_err = _make_forbidden_error()

    async def fake_deliver(telegram_id: int, config, keyboard) -> None:
        if telegram_id in BLOCKED_IDS:
            raise forbidden_err

    monkeypatch.setattr(service, '_deliver_message', fake_deliver)
    # Disable progress DB updates — not needed for this unit test
    monkeypatch.setattr(service, '_update_progress', AsyncMock())

    cancel_event = asyncio.Event()
    config = _make_config()

    result = await service._send_batched(
        broadcast_id=42,
        recipient_ids=ALL_IDS,
        config=config,
        keyboard=None,
        cancel_event=cancel_event,
    )

    sent_count, failed_count, blocked_count, was_cancelled, blocked_ids = result

    assert was_cancelled is False
    assert blocked_count == len(BLOCKED_IDS)
    assert sent_count == len(ALL_IDS) - len(BLOCKED_IDS)
    assert failed_count == 0
    assert set(blocked_ids) == BLOCKED_IDS, (
        f"Expected blocked_ids={BLOCKED_IDS}, got {set(blocked_ids)}"
    )


@pytest.mark.asyncio
async def test_send_batched_empty_blocked_when_all_succeed(monkeypatch):
    """When no messages are blocked, the 5th element must be an empty list."""
    ALL_IDS = [2001, 2002]

    service = BroadcastService()

    async def fake_deliver(telegram_id: int, config, keyboard) -> None:
        pass  # All succeed

    monkeypatch.setattr(service, '_deliver_message', fake_deliver)
    monkeypatch.setattr(service, '_update_progress', AsyncMock())

    cancel_event = asyncio.Event()

    result = await service._send_batched(
        broadcast_id=43,
        recipient_ids=ALL_IDS,
        config=_make_config(),
        keyboard=None,
        cancel_event=cancel_event,
    )

    _, _, _, was_cancelled, blocked_ids = result

    assert was_cancelled is False
    assert blocked_ids == []


@pytest.mark.asyncio
async def test_send_batched_returns_partial_blocked_ids_on_cancel(monkeypatch):
    """When broadcast is cancelled mid-run, blocked_ids collected so far must still be returned."""
    BLOCKED_IDS = {3001}
    # Two batches: first batch has 3001 (blocked); cancel fires before second batch
    ALL_IDS = [3001, 3002, 3003]

    import app.services.broadcast_service as svc_mod

    # Patch batch size to 1 so each recipient is its own batch — makes cancel timing deterministic
    monkeypatch.setattr(svc_mod, '_TG_BATCH_SIZE', 1)
    monkeypatch.setattr(svc_mod, '_TG_BATCH_DELAY', 0)

    service = BroadcastService()
    forbidden_err = _make_forbidden_error()

    call_count = 0

    async def fake_deliver(telegram_id: int, config, keyboard) -> None:
        nonlocal call_count
        call_count += 1
        if telegram_id in BLOCKED_IDS:
            raise forbidden_err
        # After first real delivery, trigger cancel so the run stops at batch 2
        cancel_event.set()

    monkeypatch.setattr(service, '_deliver_message', fake_deliver)
    monkeypatch.setattr(service, '_update_progress', AsyncMock())
    # _mark_cancelled does a DB write — stub it out
    monkeypatch.setattr(service, '_mark_cancelled', AsyncMock())

    cancel_event = asyncio.Event()

    result = await service._send_batched(
        broadcast_id=44,
        recipient_ids=ALL_IDS,
        config=_make_config(),
        keyboard=None,
        cancel_event=cancel_event,
    )

    _, _, _, was_cancelled, blocked_ids = result

    assert was_cancelled is True
    # 3001 was processed and blocked before cancellation
    assert 3001 in blocked_ids
