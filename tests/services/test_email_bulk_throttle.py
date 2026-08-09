"""Промо-рассылка не должна топить письма входа.

Прод: Яндекс начал отбивать письма промо-рассылки как спам (554 5.7.1), причём
выборочно — gmail и vk принимались всегда, yandex и mail.ru через раз. Значит
подпись домена в порядке (иначе резало бы всё подряд), а режет наш собственный
исходящий антиспам за темп и однотипность.

Проблема глубже темпа: ограничитель был один на все письма. Пока идёт рассылка,
за ней в очереди стоит код подтверждения входа — и человек не может войти
из-за промо-акции. Это надо разводить.
"""

import importlib
import time

import pytest

from app.config import settings


# В пакете имя email_service занято экземпляром сервиса, а нам нужен сам модуль
# с его глобальным ограничителем темпа.
svc = importlib.import_module('app.cabinet.services.email_service')


@pytest.fixture(autouse=True)
def reset_clock(monkeypatch):
    monkeypatch.setattr(svc, '_LAST_SEND_AT', 0.0, raising=False)
    yield
    monkeypatch.setattr(svc, '_LAST_SEND_AT', 0.0, raising=False)


class TestSettings:
    def test_bulk_has_its_own_interval(self):
        assert hasattr(settings, 'SMTP_BULK_MIN_SEND_INTERVAL_MS')

    def test_bulk_is_slower_than_transactional(self):
        """Иначе разведение бессмысленно."""
        assert settings.SMTP_BULK_MIN_SEND_INTERVAL_MS > settings.SMTP_MIN_SEND_INTERVAL_MS

    def test_spam_rejection_has_a_cooldown(self):
        assert hasattr(settings, 'SMTP_SPAM_REJECT_COOLDOWN_S')


class TestThrottle:
    def test_transactional_does_not_wait_the_bulk_interval(self, monkeypatch):
        """Код входа не должен стоять в очереди за промо-рассылкой."""
        slept: list[float] = []
        monkeypatch.setattr(svc.time, 'sleep', slept.append)
        monkeypatch.setattr(settings, 'SMTP_MIN_SEND_INTERVAL_MS', 2000)
        monkeypatch.setattr(settings, 'SMTP_BULK_MIN_SEND_INTERVAL_MS', 10000)
        monkeypatch.setattr(svc, '_LAST_SEND_AT', time.monotonic(), raising=False)

        svc._throttle_send(bulk=False)

        assert slept, 'какая-то пауза быть должна'
        assert max(slept) <= 2.0, 'транзакционное письмо ждёт только свой интервал'

    def test_bulk_waits_its_longer_interval(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(svc.time, 'sleep', slept.append)
        monkeypatch.setattr(settings, 'SMTP_MIN_SEND_INTERVAL_MS', 2000)
        monkeypatch.setattr(settings, 'SMTP_BULK_MIN_SEND_INTERVAL_MS', 10000)
        monkeypatch.setattr(svc, '_LAST_SEND_AT', time.monotonic(), raising=False)

        svc._throttle_send(bulk=True)

        assert max(slept) > 2.0, 'массовая отправка должна идти заметно реже'


class TestSpamCooldown:
    def test_cooldown_pushes_the_next_send_away(self, monkeypatch):
        """После отказа за спам нельзя продолжать в том же темпе."""
        monkeypatch.setattr(settings, 'SMTP_SPAM_REJECT_COOLDOWN_S', 60)
        monkeypatch.setattr(svc, '_LAST_SEND_AT', 0.0, raising=False)

        svc._apply_spam_cooldown()

        assert svc._LAST_SEND_AT > time.monotonic(), 'следующая отправка должна подождать'


class TestSendEmailSignature:
    def test_send_email_accepts_bulk_flag(self):
        import inspect

        signature = inspect.signature(svc.EmailService.send_email)

        assert 'bulk' in signature.parameters
        assert signature.parameters['bulk'].default is False, 'по умолчанию письмо транзакционное'
