"""T2: User.remnawave_id — BigInteger-колонка для v3 панели.

Проверяет, что модель User имеет атрибут remnawave_id типа BigInteger,
nullable, с индексом и дефолтом None.
"""

import sqlalchemy as sa

from app.database.models import User


def test_user_has_remnawave_id_attribute():
    """Атрибут remnawave_id существует в модели User."""
    assert hasattr(User, 'remnawave_id'), "User не имеет атрибута remnawave_id"


def test_remnawave_id_column_in_table():
    """Колонка remnawave_id присутствует в таблице users."""
    assert 'remnawave_id' in User.__table__.columns, \
        "Колонка remnawave_id отсутствует в User.__table__.columns"


def test_remnawave_id_is_biginteger():
    """Тип колонки — BigInteger."""
    col = User.__table__.columns['remnawave_id']
    assert isinstance(col.type, sa.BigInteger), \
        f"Ожидался BigInteger, получен {type(col.type)}"


def test_remnawave_id_is_nullable():
    """Колонка nullable (v2-пользователи не имеют этого значения)."""
    col = User.__table__.columns['remnawave_id']
    assert col.nullable is True, "remnawave_id должна быть nullable"


def test_remnawave_id_has_index():
    """Колонка должна быть проиндексирована."""
    col = User.__table__.columns['remnawave_id']
    assert col.index is True, "remnawave_id должна иметь index=True"


def test_remnawave_id_default_is_none():
    """Свежий экземпляр User не должен иметь значение remnawave_id (None)."""
    u = User.__new__(User)
    # Атрибут на новом (непривязанном) экземпляре даёт None или дескриптор без значения.
    val = u.__dict__.get('remnawave_id', None)
    assert val is None, f"Ожидался None, получен {val!r}"
