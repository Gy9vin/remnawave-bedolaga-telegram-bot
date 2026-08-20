"""Админов/спец-роли (role_level > 0) не должно выкидывать из кабинета по
короткому access-TTL: для них выдаётся длинный access-токен, обычным юзерам —
прежний короткий.
"""
from __future__ import annotations

import jwt as pyjwt
import pytest

from app.cabinet.auth.jwt_handler import create_access_token
from app.config import settings


def _ttl_minutes(token: str) -> float:
    payload = pyjwt.decode(token, options={'verify_signature': False})
    return (payload['exp'] - payload['iat']) / 60.0


def test_regular_user_keeps_short_ttl():
    ttl = _ttl_minutes(create_access_token(1, role_level=0))
    assert abs(ttl - settings.get_cabinet_access_token_expire_minutes()) < 2


def test_admin_gets_long_ttl():
    ttl = _ttl_minutes(create_access_token(1, role_level=1))
    assert ttl >= settings.get_cabinet_admin_access_token_expire_minutes() - 2
    # И это заметно больше обычного короткого TTL.
    assert ttl > settings.get_cabinet_access_token_expire_minutes() * 10


def test_admin_ttl_falls_back_to_common_when_disabled(monkeypatch):
    """CABINET_ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES <= 0 → используется общий TTL."""
    monkeypatch.setattr(settings, 'CABINET_ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES', 0, raising=False)
    ttl = _ttl_minutes(create_access_token(1, role_level=5))
    assert abs(ttl - settings.get_cabinet_access_token_expire_minutes()) < 2
