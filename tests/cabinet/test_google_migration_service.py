import pytest

from app.services import google_migration_service as gm


def test_build_invite_email_contains_link():
    subject, html = gm.build_invite_email('https://cab.example/reset-password?token=abc', 'Иван')
    assert subject
    assert 'https://cab.example/reset-password?token=abc' in html
    assert 'Друзья' in html
    # placeholders fully substituted
    assert '{{set_password_url}}' not in html


@pytest.mark.asyncio
async def test_start_is_single_flight(monkeypatch):
    service = gm.GoogleMigrationService()
    service._status.running = True  # simulate in-progress run
    started = await service.start()
    assert started is False


@pytest.mark.asyncio
async def test_send_test_to_email_empty_returns_not_found():
    service = gm.GoogleMigrationService()
    assert await service.send_test_to_email('') == {'found': False, 'sent': False}
    assert await service.send_test_to_email('   ') == {'found': False, 'sent': False}
