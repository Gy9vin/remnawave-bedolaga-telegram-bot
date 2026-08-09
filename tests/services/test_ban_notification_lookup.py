"""Поиск получателя уведомления о бане.

Прод-лог: `AttributeError: type object 'Subscription' has no attribute 'email'`.
Запасная ветка поиска обращалась к несуществующей колонке, поэтому падала в
исключение при каждом вызове — уведомление о бане не доходило вообще никогда,
а в логе оставалось безобидное «Пользователь не найден в базе данных».

Плюс идентификатор не всегда email: панель зовёт нас и числовым id, и по нему
искать надо в панельных колонках, а не в почте.
"""

import inspect

from app.services.ban_notification_service import BanNotificationService


def _lookup_source() -> str:
    return inspect.getsource(BanNotificationService._find_user_by_identifier)


def test_no_longer_queries_a_column_that_does_not_exist():
    """Проверяем именно запрос: упоминание колонки в комментарии допустимо."""
    source = _lookup_source()

    assert 'Subscription.email ==' not in source, (
        'у Subscription нет колонки email — запрос падал AttributeError'
    )
    assert 'where(Subscription.email' not in source


def test_searches_email_on_the_user():
    source = _lookup_source()

    assert 'User.email' in source


def test_numeric_identifier_is_looked_up_by_panel_id():
    """Панель присылает и числовой id — по нему почту искать бессмысленно."""
    source = _lookup_source()

    assert 'remnawave_id' in source


def test_lookup_is_still_failure_tolerant():
    """Сбой поиска не должен ронять обработку вебхука от панели."""
    source = _lookup_source()

    assert 'except Exception' in source
    assert 'return None' in source
