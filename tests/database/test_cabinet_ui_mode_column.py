"""Колонка персонального выбора интерфейса.

NULL — осознанное состояние «человек не выбирал», а не отсутствие данных:
такие пользователи слушают глобальный дефолт и подхватывают его смену.
Поэтому у колонки нет server_default и она nullable.
"""

from sqlalchemy import String

from app.database.models import User


def test_user_has_cabinet_ui_mode_column():
    column = User.__table__.columns['cabinet_ui_mode']
    assert isinstance(column.type, String)
    assert column.type.length == 16
    assert column.nullable is True
    assert column.server_default is None
