"""Поиск получателя, за которого платят.

Плательщик вводит то, что знает: @ник, telegram id или email. Разбирать это надо
на нашей стороне — заставлять человека выбирать тип идентификатора значит
плодить вопросы в поддержку.

Отказ всегда выглядит одинаково — «не найдено». Иначе по ответам можно перебором
выяснять, кто наш клиент, а кто в чёрном списке.
"""

from types import SimpleNamespace

import pytest

from app.services import recipient_lookup as svc


ACTIVE = 'active'


def _user(**overrides):
    base = {
        'id': 42,
        'telegram_id': 555000111,
        'username': 'vasya',
        'email': 'vasya@example.com',
        'status': ACTIVE,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestQueryParsing:
    @pytest.mark.parametrize(
        ('query', 'expected'),
        [
            ('@vasya', ('username', 'vasya')),
            ('vasya', ('username', 'vasya')),
            ('  @Vasya  ', ('username', 'Vasya')),
            ('555000111', ('telegram_id', '555000111')),
            ('vasya@example.com', ('email', 'vasya@example.com')),
            ('  VASYA@Example.COM ', ('email', 'VASYA@Example.COM')),
        ],
    )
    def test_recognises_every_supported_form(self, query, expected):
        assert svc.classify_query(query) == expected

    @pytest.mark.parametrize('query', ['', '   ', '@', 'a b', 'https://t.me/vasya'])
    def test_rejects_junk(self, query):
        assert svc.classify_query(query) is None


class TestResolve:
    @pytest.mark.asyncio
    async def test_finds_by_username_ignoring_case(self, monkeypatch):
        user = _user()
        seen = {}

        async def fake_by_username(db, username):
            seen['username'] = username
            return user

        monkeypatch.setattr(svc, '_find_by_username', fake_by_username)
        monkeypatch.setattr(svc, '_is_blacklisted', _never_blacklisted)

        assert await svc.resolve_recipient(None, '@VaSyA', payer_id=1) is user
        assert seen['username'] == 'VaSyA'

    @pytest.mark.asyncio
    async def test_finds_by_telegram_id(self, monkeypatch):
        user = _user()
        monkeypatch.setattr(svc, '_find_by_telegram_id', _returning(user))
        monkeypatch.setattr(svc, '_is_blacklisted', _never_blacklisted)

        assert await svc.resolve_recipient(None, '555000111', payer_id=1) is user

    @pytest.mark.asyncio
    async def test_finds_by_email(self, monkeypatch):
        user = _user()
        monkeypatch.setattr(svc, '_find_by_email', _returning(user))
        monkeypatch.setattr(svc, '_is_blacklisted', _never_blacklisted)

        assert await svc.resolve_recipient(None, 'vasya@example.com', payer_id=1) is user

    @pytest.mark.asyncio
    async def test_refuses_yourself(self, monkeypatch):
        """Себе платят обычным продлением — тут это ошибка ввода."""
        user = _user(id=1)
        monkeypatch.setattr(svc, '_find_by_username', _returning(user))
        monkeypatch.setattr(svc, '_is_blacklisted', _never_blacklisted)

        with pytest.raises(svc.RecipientIsPayerError):
            await svc.resolve_recipient(None, '@vasya', payer_id=1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize('status', ['blocked', 'deleted'])
    async def test_refuses_inactive_accounts(self, monkeypatch, status):
        monkeypatch.setattr(svc, '_find_by_username', _returning(_user(status=status)))
        monkeypatch.setattr(svc, '_is_blacklisted', _never_blacklisted)

        assert await svc.resolve_recipient(None, '@vasya', payer_id=1) is None

    @pytest.mark.asyncio
    async def test_refuses_blacklisted_indistinguishably(self, monkeypatch):
        """Ответ не должен отличаться от «не найдено»."""
        monkeypatch.setattr(svc, '_find_by_username', _returning(_user()))

        async def blacklisted(user):
            return True

        monkeypatch.setattr(svc, '_is_blacklisted', blacklisted)

        assert await svc.resolve_recipient(None, '@vasya', payer_id=1) is None

    @pytest.mark.asyncio
    async def test_junk_query_returns_none_without_touching_db(self, monkeypatch):
        async def explode(*args, **kwargs):
            raise AssertionError('мусорный запрос не должен ходить в базу')

        monkeypatch.setattr(svc, '_find_by_username', explode)
        monkeypatch.setattr(svc, '_find_by_telegram_id', explode)
        monkeypatch.setattr(svc, '_find_by_email', explode)

        assert await svc.resolve_recipient(None, 'https://t.me/vasya', payer_id=1) is None


def _returning(value):
    async def _inner(db, _query):
        return value

    return _inner


async def _never_blacklisted(_user):
    return False
