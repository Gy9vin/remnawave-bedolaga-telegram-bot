def test_broadcast_history_has_blocked_user_ids():
    from app.database.models import BroadcastHistory
    assert 'blocked_user_ids' in BroadcastHistory.__table__.columns
