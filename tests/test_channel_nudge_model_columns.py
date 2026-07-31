"""Smoke test: new columns exist on the ORM models before migration runs."""
import pytest


def test_required_channel_has_is_main_column():
    from app.database.models import RequiredChannel
    assert hasattr(RequiredChannel, 'is_main'), 'RequiredChannel.is_main missing'


def test_required_channel_has_last_post_columns():
    from app.database.models import RequiredChannel
    for col in ('last_post_message_id', 'last_post_link', 'last_post_title', 'last_post_at'):
        assert hasattr(RequiredChannel, col), f'RequiredChannel.{col} missing'


def test_user_has_last_seen_channel_post_id():
    from app.database.models import User
    assert hasattr(User, 'last_seen_channel_post_id'), 'User.last_seen_channel_post_id missing'
