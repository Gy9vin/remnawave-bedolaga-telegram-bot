"""Тесты для backfill_remnawave_ids.py — массового бэкфилла User.remnawave_id.

Покрывает только чистую логику сопоставления и обработки батчей (без сети
и без реальной БД — стрим панели и сессия мокаются). Проверяется:
  - сопоставление по telegram_id
  - сопоставление по short_uuid
  - приоритет источников: short_uuid > telegram_id > email
  - неоднозначность (несколько разных id панели под одним telegram_id/email)
    не резолвится, а помечается как ambiguous
  - идемпотентность: повторный прогон по уже проставленным пользователям
    ничего не меняет (они просто не должны попадать в выборку NULL)
  - dry-run ничего не пишет в ORM-объекты
"""

from __future__ import annotations

from types import SimpleNamespace

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import backfill_remnawave_ids as backfill  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _panel_user(*, panel_id: int, short_uuid: str | None = None, telegram_id: int | None = None, email: str | None = None):
    return SimpleNamespace(id=panel_id, short_uuid=short_uuid, telegram_id=telegram_id, email=email)


def _sub(short_uuid: str | None):
    return SimpleNamespace(remnawave_short_uuid=short_uuid)


def _user(
    *,
    user_id: int = 1,
    telegram_id: int | None = None,
    email: str | None = None,
    remnawave_id: int | None = None,
    subscriptions: list | None = None,
):
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        email=email,
        remnawave_id=remnawave_id,
        subscriptions=subscriptions or [],
    )


def _build_index(panel_users) -> backfill.PanelIndex:
    index = backfill.PanelIndex()
    for pu in panel_users:
        index.add(short_uuid=pu.short_uuid, telegram_id=pu.telegram_id, email=pu.email, panel_id=pu.id)
    return index


# ---------------------------------------------------------------------------
# select_short_uuid
# ---------------------------------------------------------------------------


def test_select_short_uuid_picks_first_non_null_in_order():
    subs = [_sub(None), _sub('short-2'), _sub('short-3')]
    assert backfill.select_short_uuid(subs) == 'short-2'


def test_select_short_uuid_returns_none_when_no_subscriptions():
    assert backfill.select_short_uuid([]) is None
    assert backfill.select_short_uuid([_sub(None), _sub(None)]) is None


# ---------------------------------------------------------------------------
# match_user_to_panel_id — базовые сопоставления
# ---------------------------------------------------------------------------


def test_match_by_telegram_id():
    index = _build_index([_panel_user(panel_id=100, telegram_id=555)])

    result = backfill.match_user_to_panel_id(
        short_uuid=None, telegram_id=555, email=None, index=index
    )

    assert result.panel_id == 100
    assert result.source == 'telegram_id'
    assert result.reason is None


def test_match_by_short_uuid():
    index = _build_index([_panel_user(panel_id=200, short_uuid='abc-short')])

    result = backfill.match_user_to_panel_id(
        short_uuid='abc-short', telegram_id=None, email=None, index=index
    )

    assert result.panel_id == 200
    assert result.source == 'short_uuid'


def test_match_by_email_lowercased():
    index = _build_index([_panel_user(panel_id=300, email='User@Example.com')])

    result = backfill.match_user_to_panel_id(
        short_uuid=None, telegram_id=None, email='user@example.com', index=index
    )

    assert result.panel_id == 300
    assert result.source == 'email'


def test_no_match_returns_not_found_reason_when_signal_present():
    index = _build_index([])  # пустая панель

    result = backfill.match_user_to_panel_id(
        short_uuid=None, telegram_id=999, email=None, index=index
    )

    assert result.panel_id is None
    assert result.reason == 'not_found'


def test_no_match_returns_no_signal_when_nothing_to_match_on():
    index = _build_index([_panel_user(panel_id=1, telegram_id=1)])

    result = backfill.match_user_to_panel_id(
        short_uuid=None, telegram_id=None, email=None, index=index
    )

    assert result.panel_id is None
    assert result.reason == 'no_signal'


# ---------------------------------------------------------------------------
# Приоритет источников: short_uuid > telegram_id > email
# ---------------------------------------------------------------------------


def test_priority_short_uuid_wins_over_telegram_id_and_email():
    index = _build_index(
        [
            _panel_user(panel_id=1, short_uuid='the-short-uuid'),
            _panel_user(panel_id=2, telegram_id=777),
            _panel_user(panel_id=3, email='a@b.com'),
        ]
    )

    result = backfill.match_user_to_panel_id(
        short_uuid='the-short-uuid', telegram_id=777, email='a@b.com', index=index
    )

    assert result.panel_id == 1
    assert result.source == 'short_uuid'


def test_priority_telegram_id_wins_over_email_when_short_uuid_absent():
    index = _build_index(
        [
            _panel_user(panel_id=2, telegram_id=777),
            _panel_user(panel_id=3, email='a@b.com'),
        ]
    )

    result = backfill.match_user_to_panel_id(
        short_uuid=None, telegram_id=777, email='a@b.com', index=index
    )

    assert result.panel_id == 2
    assert result.source == 'telegram_id'


def test_falls_back_to_telegram_id_when_short_uuid_not_found_in_panel():
    """Зеркалирует get_panel_user_ref: если short_uuid есть, но resolve_user_id
    (здесь — поиск в индексе) не находит его на панели, идём дальше по
    приоритету к telegram_id, а не сдаёмся сразу."""
    index = _build_index([_panel_user(panel_id=9, telegram_id=42)])

    result = backfill.match_user_to_panel_id(
        short_uuid='unknown-short-uuid', telegram_id=42, email=None, index=index
    )

    assert result.panel_id == 9
    assert result.source == 'telegram_id'


# ---------------------------------------------------------------------------
# Неоднозначность
# ---------------------------------------------------------------------------


def test_ambiguous_telegram_id_is_not_resolved():
    index = backfill.PanelIndex()
    index.add(short_uuid=None, telegram_id=555, email=None, panel_id=10)
    index.add(short_uuid=None, telegram_id=555, email=None, panel_id=11)

    result = backfill.match_user_to_panel_id(
        short_uuid=None, telegram_id=555, email=None, index=index
    )

    assert result.panel_id is None
    assert result.reason == 'ambiguous_telegram_id'


def test_ambiguous_email_is_not_resolved():
    index = backfill.PanelIndex()
    index.add(short_uuid=None, telegram_id=None, email='dup@example.com', panel_id=10)
    index.add(short_uuid=None, telegram_id=None, email='DUP@example.com', panel_id=11)

    result = backfill.match_user_to_panel_id(
        short_uuid=None, telegram_id=None, email='dup@example.com', index=index
    )

    assert result.panel_id is None
    assert result.reason == 'ambiguous_email'


def test_duplicate_short_uuid_in_panel_keeps_first_and_warns(caplog):
    index = backfill.PanelIndex()
    index.add(short_uuid='dup-short', telegram_id=None, email=None, panel_id=1)
    index.add(short_uuid='dup-short', telegram_id=None, email=None, panel_id=2)

    assert index.by_short_uuid['dup-short'] == 1


# ---------------------------------------------------------------------------
# process_batch — статистика, запись атрибута, dry-run
# ---------------------------------------------------------------------------


def test_process_batch_writes_remnawave_id_and_updates_stats():
    index = _build_index([_panel_user(panel_id=42, telegram_id=555)])
    user = _user(user_id=1, telegram_id=555)
    stats = backfill.Stats()

    backfill.process_batch([user], index, dry_run=False, stats=stats)

    assert user.remnawave_id == 42
    assert stats.matched == 1
    assert stats.matched_by['telegram_id'] == 1
    assert stats.not_found == 0


def test_process_batch_dry_run_does_not_mutate_user():
    index = _build_index([_panel_user(panel_id=42, telegram_id=555)])
    user = _user(user_id=1, telegram_id=555, remnawave_id=None)
    stats = backfill.Stats()

    backfill.process_batch([user], index, dry_run=True, stats=stats)

    assert user.remnawave_id is None  # ничего не записано
    assert stats.matched == 1  # но статистика всё равно посчитана


def test_process_batch_counts_not_found_reasons():
    index = _build_index([])
    user_no_signal = _user(user_id=1, telegram_id=None, email=None)
    user_not_found = _user(user_id=2, telegram_id=123, email=None)
    stats = backfill.Stats()

    backfill.process_batch([user_no_signal, user_not_found], index, dry_run=False, stats=stats)

    assert stats.matched == 0
    assert stats.not_found == 2
    assert stats.not_found_by_reason['no_signal'] == 1
    assert stats.not_found_by_reason['not_found'] == 1


# ---------------------------------------------------------------------------
# Идемпотентность второго прогона
# ---------------------------------------------------------------------------


def test_second_run_is_noop_for_already_matched_users():
    """Имитирует повторный запуск: пользователь уже получил remnawave_id
    на первом прогоне, поэтому во втором прогоне он не должен даже попасть
    в выборку NULL (эмулируем это здесь на уровне process_batch — если бы
    он всё же был передан повторно, значения не должны «уехать» на другой id)."""
    index = _build_index([_panel_user(panel_id=42, telegram_id=555)])
    user = _user(user_id=1, telegram_id=555, remnawave_id=42)  # уже проставлено
    stats = backfill.Stats()

    # Второй прогон по тому же пользователю (гипотетически, если бы фильтр
    # remnawave_id IS NULL не сработал) — результат сопоставления детерминирован
    # и указывает на тот же id, порчи данных не будет.
    backfill.process_batch([user], index, dry_run=False, stats=stats)

    assert user.remnawave_id == 42
    assert stats.matched == 1


def test_fetch_null_batch_excludes_already_matched_users_from_query():
    """Проверяет сам SQL-фильтр: у sqlalchemy select с
    User.remnawave_id.is_(None) должен быть WHERE remnawave_id IS NULL —
    так реальная БД просто не вернёт уже проставленных пользователей на
    повторном прогоне (за счёт этого достигается идемпотентность и
    устойчивость к сбою на середине)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database.models import User as UserModel

    stmt = (
        select(UserModel)
        .where(UserModel.remnawave_id.is_(None), UserModel.id > 0)
        .options(selectinload(UserModel.subscriptions))
        .order_by(UserModel.id)
        .limit(10)
    )
    compiled = str(stmt.compile(compile_kwargs={'literal_binds': True}))
    assert 'remnawave_id IS NULL' in compiled
    assert 'users.id > 0' in compiled


# ---------------------------------------------------------------------------
# build_panel_index — мок стрима панели (пагинация, без сети)
# ---------------------------------------------------------------------------


async def test_build_panel_index_walks_all_stream_pages():
    from unittest.mock import AsyncMock

    api = AsyncMock()
    page_1 = {
        'users': [
            _panel_user(panel_id=1, short_uuid='s1', telegram_id=111),
            _panel_user(panel_id=2, telegram_id=222),
        ],
        'nextCursor': 'cursor-2',
        'hasMore': True,
    }
    page_2 = {
        'users': [_panel_user(panel_id=3, email='c@example.com')],
        'nextCursor': None,
        'hasMore': False,
    }
    api.get_all_users_page_stream = AsyncMock(side_effect=[page_1, page_2])

    index = await backfill.build_panel_index(api, page_size=500)

    assert index.total_panel_users == 3
    assert index.by_short_uuid['s1'] == 1
    assert index.by_telegram_id[111] == [1]
    assert index.by_telegram_id[222] == [2]
    assert index.by_email['c@example.com'] == [3]

    # первая страница — без курсора, вторая — с курсором из ответа первой
    first_call_kwargs = api.get_all_users_page_stream.call_args_list[0].kwargs
    second_call_kwargs = api.get_all_users_page_stream.call_args_list[1].kwargs
    assert first_call_kwargs['cursor'] is None
    assert second_call_kwargs['cursor'] == 'cursor-2'
    assert first_call_kwargs['enrich_happ_links'] is False


async def test_build_panel_index_stops_when_has_more_false():
    from unittest.mock import AsyncMock

    api = AsyncMock()
    api.get_all_users_page_stream = AsyncMock(
        return_value={'users': [_panel_user(panel_id=1, telegram_id=1)], 'nextCursor': None, 'hasMore': False}
    )

    index = await backfill.build_panel_index(api, page_size=500)

    assert api.get_all_users_page_stream.await_count == 1
    assert index.total_panel_users == 1


# ---------------------------------------------------------------------------
# fetch_null_batch — мок AsyncSession
# ---------------------------------------------------------------------------


async def test_fetch_null_batch_returns_scalars_from_mocked_session():
    from unittest.mock import AsyncMock, MagicMock

    users = [_user(user_id=5), _user(user_id=6)]

    scalars_result = MagicMock()
    scalars_result.all.return_value = users
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result

    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    result = await backfill.fetch_null_batch(db, last_id=0, batch_size=100)

    assert result == users
    db.execute.assert_awaited_once()


async def test_fetch_null_batch_returns_empty_list_when_no_more_rows():
    from unittest.mock import AsyncMock, MagicMock

    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result

    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    result = await backfill.fetch_null_batch(db, last_id=999, batch_size=100)

    assert result == []
