"""Модель оплаты за другого и её миграция.

Запись нужна прежде всего для случая «на балансе не хватило»: платёж уходит во
внешнюю платёжку и применяется по вебхуку, а до тех пор надо помнить, кому, за
что и почём.
"""

import re
from pathlib import Path

from app.database.models import SponsoredPayment, SponsoredPaymentStatus


MIGRATION = Path('migrations/alembic/versions/9028_sponsored_payments.py')


def test_columns_match_the_spec():
    columns = set(SponsoredPayment.__table__.columns.keys())

    assert columns == {
        'id',
        'payer_user_id',
        'recipient_user_id',
        'subscription_id',
        'tariff_id',
        'period_days',
        'amount_kopeks',
        'status',
        'payment_method',
        'payment_id',
        'created_at',
        'paid_at',
        'applied_at',
    }


def test_payer_and_recipient_are_required():
    table = SponsoredPayment.__table__

    assert not table.columns['payer_user_id'].nullable
    assert not table.columns['recipient_user_id'].nullable
    assert not table.columns['amount_kopeks'].nullable


def test_subscription_is_optional_for_users_without_one():
    assert SponsoredPayment.__table__.columns['subscription_id'].nullable


def test_statuses_cover_the_lifecycle():
    assert {s.value for s in SponsoredPaymentStatus} == {'pending', 'paid', 'applied', 'failed', 'expired'}


def test_migration_follows_our_numbering():
    source = MIGRATION.read_text()

    assert re.search(r"^revision: str = '9028'", source, re.M)
    assert re.search(r"^down_revision: Union\[str, Sequence\[str\], None\] = '9027'", source, re.M)


def test_migration_guards_double_application_by_payment_id():
    """Повтор вебхука не должен применяться дважды даже при гонке воркеров."""
    source = MIGRATION.read_text()

    assert 'ux_sponsored_payments_payment_id' in source
    assert 'unique=True' in source


def test_migration_is_reversible():
    source = MIGRATION.read_text()

    assert 'def downgrade()' in source
    assert "op.drop_table('sponsored_payments')" in source
