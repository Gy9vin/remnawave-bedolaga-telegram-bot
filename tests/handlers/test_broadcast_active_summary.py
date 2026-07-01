"""Unit tests for _format_blocked_active_summary helper."""

from app.handlers.admin.messages import _format_blocked_active_summary


def test_empty_list_returns_empty_string():
    assert _format_blocked_active_summary([]) == ''


def test_single_user_with_username_and_tariff():
    users = [
        {
            'telegram_id': 123,
            'username': 'testuser',
            'tariff_name': 'Pro',
            'days_left': 5,
        }
    ]
    result = _format_blocked_active_summary(users)
    assert '@testuser' in result
    assert 'Pro' in result
    assert 'дн.' in result
    assert 'Из них с активной подпиской: 1' in result


def test_twelve_users_truncates_to_ten_and_shows_note():
    users = [
        {
            'telegram_id': i,
            'username': f'user{i}',
            'tariff_name': 'Basic',
            'days_left': i,
        }
        for i in range(1, 13)
    ]
    result = _format_blocked_active_summary(users)
    assert '…полный список в кабинете' in result
    # Exactly 10 bullet lines (lines starting with '  • ')
    bullet_lines = [line for line in result.splitlines() if line.startswith('  • ')]
    assert len(bullet_lines) == 10


def test_user_without_username_uses_id_form():
    users = [
        {
            'telegram_id': 999,
            'username': None,
            'tariff_name': 'VIP',
            'days_left': 3,
        }
    ]
    result = _format_blocked_active_summary(users)
    assert 'id999' in result
    assert '@' not in result
