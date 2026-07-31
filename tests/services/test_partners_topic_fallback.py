"""Категория PARTNERS (выводы средств) должна фолбэчить на старый ключ
REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID, если новый
ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID не задан — иначе кабинетные заявки на
вывод уходят в общий топик вместо раздела выводов (регресс с 2026-03-18).
"""
from unittest.mock import MagicMock

from app.config import settings
from app.services.admin_notification_service import (
    AdminNotificationService,
    NotificationCategory,
)


def test_partners_topic_falls_back_to_legacy_withdrawal_key(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID', None, raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID', 424242, raising=False)
    service = AdminNotificationService(MagicMock())
    assert service.category_topics[NotificationCategory.PARTNERS] == 424242


def test_partners_topic_prefers_new_key_when_both_set(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID', 111, raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID', 222, raising=False)
    service = AdminNotificationService(MagicMock())
    assert service.category_topics[NotificationCategory.PARTNERS] == 111


def test_partners_topic_none_when_neither_set(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID', None, raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID', None, raising=False)
    service = AdminNotificationService(MagicMock())
    assert service.category_topics[NotificationCategory.PARTNERS] is None
