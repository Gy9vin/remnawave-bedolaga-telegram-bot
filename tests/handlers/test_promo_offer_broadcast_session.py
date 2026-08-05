"""Отчёт о рассылке промопредложения не должен падать на мёртвом соединении.

Прод: рассылка по сегменту отработала, все сообщения ушли — а хендлер упал
`InterfaceError: the underlying connection is closed` на самом последнем
запросе, когда перечитывал шаблон для кнопки «К предложению». Админ увидел
ошибку вместо отчёта и не понял, ушла рассылка или нет.

Причина не в самой базе: сессия запроса всё время рассылки (минуты) висит с
открытой транзакцией, а у соединений выставлен
`idle_in_transaction_session_timeout=5м` — postgres такую сессию убивает сам.
Поэтому перечитывать шаблон нужно новой сессией, а её сбой не должен стоить
админу отчёта.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.admin import promo_offers


class _Session:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.closed = False

    async def __aenter__(self):
        if self.fail:
            raise RuntimeError('the underlying connection is closed')
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        return False


@pytest.fixture
def fresh_session(monkeypatch):
    made: list[_Session] = []

    def factory(*, fail=False):
        def _make():
            session = _Session(fail=fail)
            made.append(session)
            return session

        monkeypatch.setattr('app.database.database.AsyncSessionLocal', _make)
        return made

    return factory


@pytest.mark.asyncio
async def test_template_is_reloaded_with_a_fresh_session(monkeypatch, fresh_session):
    made = fresh_session()
    template = SimpleNamespace(id=3, name='Скидка')
    loader = AsyncMock(return_value=template)
    monkeypatch.setattr(promo_offers, 'get_promo_offer_template_by_id', loader)

    result = await promo_offers._reload_template_after_broadcast(3)

    assert result is template
    assert len(made) == 1, 'перечитывать нужно ИМЕННО новой сессией'
    assert made[0].closed, 'сессия должна закрываться'
    assert loader.await_args.args[0] is made[0]


@pytest.mark.asyncio
async def test_dead_connection_does_not_cost_the_report(monkeypatch, fresh_session):
    """Соединение умерло — отчёт всё равно должен дойти до админа."""
    fresh_session(fail=True)
    monkeypatch.setattr(promo_offers, 'get_promo_offer_template_by_id', AsyncMock())

    assert await promo_offers._reload_template_after_broadcast(3) is None


@pytest.mark.asyncio
async def test_query_failure_is_also_survived(monkeypatch, fresh_session):
    fresh_session()
    monkeypatch.setattr(
        promo_offers,
        'get_promo_offer_template_by_id',
        AsyncMock(side_effect=RuntimeError('connection closed')),
    )

    assert await promo_offers._reload_template_after_broadcast(3) is None


@pytest.mark.asyncio
async def test_open_transaction_is_released_before_a_long_broadcast(monkeypatch):
    """Перед рассылкой транзакцию сессии запроса нужно закрыть.

    Иначе postgres убьёт её по idle_in_transaction_session_timeout, пока
    рассылка идёт, и упадёт уже следующий запрос.
    """
    db = MagicMock()
    db.commit = AsyncMock()

    await promo_offers._release_session_before_broadcast(db)

    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_failure_does_not_abort_the_broadcast(monkeypatch):
    db = MagicMock()
    db.commit = AsyncMock(side_effect=RuntimeError('already closed'))

    await promo_offers._release_session_before_broadcast(db)  # не бросает
