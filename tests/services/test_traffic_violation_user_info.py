"""Шапка уведомления о превышении трафика должна называть человека.

Прод-баг: в чат админов уходило уведомление, где из идентификации был только
`🔑 ID в панели: 15758` — ни telegram id, ни ника. Причина: блок с данными
пользователя строился ИСКЛЮЧИТЕЛЬНО по записи бота, найденной по
`users.remnawave_id`, а после миграции v2→v3 эта связь у части юзеров пустая.
При этом telegram id и логин лежат прямо в панельном ответе, по которому
нарушение и обнаружено, — их просто не использовали.
"""

from types import SimpleNamespace

import pytest

from app.services.traffic_monitoring_service import TrafficMonitoringServiceV2, TrafficViolation


def _violation(**overrides) -> TrafficViolation:
    defaults = {
        'user_id': 15758,
        'telegram_id': 111222333,
        'full_name': 'panel_login',
        'username': 'panel_login',
        'used_traffic_gb': 10.91,
        'threshold_gb': 10.0,
        'last_node_uuid': '0ff7cc8c-4d4f-4b58-b4b6-db484c6447c1',
        'last_node_name': 'DE_most_1',
        'check_type': 'fast',
        'email': 'someone@example.com',
        'short_uuid': 'abc123',
    }
    defaults.update(overrides)
    return TrafficViolation(**defaults)


class TestPanelFallback:
    def test_panel_info_names_the_user_when_bot_record_is_missing(self):
        info = TrafficMonitoringServiceV2._format_panel_user_info(_violation())

        assert '111222333' in info, 'telegram id из панели обязан попасть в уведомление'
        assert 'panel_login' in info
        assert 'someone@example.com' in info
        assert 'abc123' in info
        assert 'не найден' in info, 'админ должен видеть, что записи в боте нет'

    def test_panel_info_survives_missing_fields(self):
        info = TrafficMonitoringServiceV2._format_panel_user_info(
            _violation(telegram_id=None, username=None, full_name=None, email=None, short_uuid=None)
        )

        assert 'не найден' in info

    def test_panel_info_escapes_html(self):
        info = TrafficMonitoringServiceV2._format_panel_user_info(_violation(username='<b>x</b>'))

        assert '<b>x</b>' not in info
        assert '&lt;b&gt;x&lt;/b&gt;' in info


class TestDbUserInfo:
    def test_db_info_prefers_bot_record(self):
        db_user = SimpleNamespace(
            id=42,
            telegram_id=999,
            username='vasya',
            email='vasya@example.com',
            full_name='Вася Пупкин',
        )

        info = TrafficMonitoringServiceV2._format_db_user_info(db_user)

        assert 'Вася Пупкин' in info
        assert '999' in info
        assert '@vasya' in info
        assert '#42' in info

    def test_db_info_without_optional_fields(self):
        db_user = SimpleNamespace(id=7, telegram_id=None, username=None, email=None, full_name='Без имени')

        info = TrafficMonitoringServiceV2._format_db_user_info(db_user)

        assert '#7' in info


class TestLookupOrder:
    """Поиск записи бота: по панельному id, затем по telegram id из панели."""

    @pytest.mark.asyncio
    async def test_falls_back_to_telegram_lookup_when_panel_link_is_broken(self, monkeypatch):
        db_user = SimpleNamespace(
            id=42, telegram_id=111222333, username='vasya', email=None, full_name='Вася'
        )
        seen = {}

        async def fake_by_remnawave_id(db, panel_id):
            seen['panel_id'] = panel_id
            return None  # связь потеряна

        async def fake_by_telegram_id(db, telegram_id):
            seen['telegram_id'] = telegram_id
            return db_user

        monkeypatch.setattr(
            'app.services.traffic_monitoring_service.get_user_by_remnawave_id', fake_by_remnawave_id
        )
        monkeypatch.setattr(
            'app.services.traffic_monitoring_service.get_user_by_telegram_id', fake_by_telegram_id
        )

        info = await TrafficMonitoringServiceV2()._build_violation_user_info(_violation())

        assert seen == {'panel_id': 15758, 'telegram_id': 111222333}
        assert 'Вася' in info
        assert 'не найден' not in info

    @pytest.mark.asyncio
    async def test_db_failure_does_not_lose_the_notification(self, monkeypatch):
        async def boom(db, _value):
            raise RuntimeError('pool exhausted')

        monkeypatch.setattr('app.services.traffic_monitoring_service.get_user_by_remnawave_id', boom)

        info = await TrafficMonitoringServiceV2()._build_violation_user_info(_violation())

        assert '111222333' in info
