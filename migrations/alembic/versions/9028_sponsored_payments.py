"""таблица sponsored_payments — оплата подписки за другого человека

Плательщик оплачивает продление чужой подписки по цене получателя. Запись
нужна, когда денег на балансе не хватило: платёж уходит во внешнюю платёжку и
применяется по вебхуку, а до тех пор надо помнить, кому, за что и почём.

Отдельная таблица, а не расширение guest_purchases: на ту завязано больше
трёхсот обращений в полутора десятках файлов (лендинги, подарки, статистика
продаж, слияние аккаунтов, бэкапы, мониторинг), и новый вид записи там обязал бы
каждого потребителя его отфильтровать.

Revision ID: 9028
Revises: 9027
Create Date: 2026-08-09

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '9028'
down_revision: Union[str, Sequence[str], None] = '9027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sponsored_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('payer_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('recipient_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column(
            'subscription_id',
            sa.Integer(),
            sa.ForeignKey('subscriptions.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('period_days', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('payment_method', sa.String(length=50), nullable=True),
        sa.Column('payment_id', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_sponsored_payments_status', 'sponsored_payments', ['status'])
    op.create_index('ix_sponsored_payments_payer', 'sponsored_payments', ['payer_user_id'])
    op.create_index('ix_sponsored_payments_recipient', 'sponsored_payments', ['recipient_user_id'])
    # Уникальность внешнего платежа — единственная защита от двойного применения
    # при повторе вебхука, которая работает даже при гонке двух воркеров.
    op.create_index(
        'ux_sponsored_payments_payment_id',
        'sponsored_payments',
        ['payment_id'],
        unique=True,
        postgresql_where=sa.text('payment_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ux_sponsored_payments_payment_id', table_name='sponsored_payments')
    op.drop_index('ix_sponsored_payments_recipient', table_name='sponsored_payments')
    op.drop_index('ix_sponsored_payments_payer', table_name='sponsored_payments')
    op.drop_index('ix_sponsored_payments_status', table_name='sponsored_payments')
    op.drop_table('sponsored_payments')
