from datetime import UTC, datetime, timedelta


def test_settings_have_google_migration_defaults():
    from app.config import settings
    assert settings.GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS == 30
    assert settings.GOOGLE_AUTH_ENABLED is True


def test_token_expiry_is_far_in_future():
    from app.cabinet.auth.email_verification import get_google_migration_token_expires_at
    expires = get_google_migration_token_expires_at()
    delta = expires - datetime.now(UTC)
    assert delta > timedelta(days=29)
