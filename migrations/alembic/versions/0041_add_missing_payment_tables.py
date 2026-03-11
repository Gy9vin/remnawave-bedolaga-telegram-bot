"""Add missing payment tables and partner_applications

Revision ID: 0041
Revises: 0040
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = '0041'
down_revision: Union[str, None] = '0040'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table_name)


def upgrade() -> None:
    if not _table_exists('cryptobot_payments'):
        op.create_table(
            'cryptobot_payments',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
            sa.Column('invoice_id', sa.String(255), unique=True, nullable=False, index=True),
            sa.Column('amount', sa.String(50), nullable=False),
            sa.Column('asset', sa.String(10), nullable=False),
            sa.Column('status', sa.String(50), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('payload', sa.Text(), nullable=True),
            sa.Column('bot_invoice_url', sa.Text(), nullable=True),
            sa.Column('mini_app_invoice_url', sa.Text(), nullable=True),
            sa.Column('web_app_invoice_url', sa.Text(), nullable=True),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      onupdate=sa.func.now()),
        )

    if not _table_exists('heleket_payments'):
        op.create_table(
            'heleket_payments',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
            sa.Column('uuid', sa.String(255), unique=True, nullable=False, index=True),
            sa.Column('order_id', sa.String(128), unique=True, nullable=False, index=True),
            sa.Column('amount', sa.String(50), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False),
            sa.Column('payer_amount', sa.String(50), nullable=True),
            sa.Column('payer_currency', sa.String(10), nullable=True),
            sa.Column('exchange_rate', sa.Float(), nullable=True),
            sa.Column('discount_percent', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(50), nullable=False),
            sa.Column('payment_url', sa.Text(), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      onupdate=sa.func.now()),
        )

    if not _table_exists('mulenpay_payments'):
        op.create_table(
            'mulenpay_payments',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
            sa.Column('mulen_payment_id', sa.Integer(), nullable=True, index=True),
            sa.Column('uuid', sa.String(255), unique=True, nullable=False, index=True),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('status', sa.String(50), nullable=False, server_default='created'),
            sa.Column('is_paid', sa.Boolean(), server_default='false'),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('payment_url', sa.Text(), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('callback_payload', sa.JSON(), nullable=True),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      onupdate=sa.func.now()),
        )

    if not _table_exists('pal24_payments'):
        op.create_table(
            'pal24_payments',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
            sa.Column('bill_id', sa.String(255), unique=True, nullable=False, index=True),
            sa.Column('order_id', sa.String(255), nullable=True, index=True),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('type', sa.String(20), nullable=False, server_default='normal'),
            sa.Column('status', sa.String(50), nullable=False, server_default='NEW'),
            sa.Column('is_active', sa.Boolean(), server_default='true'),
            sa.Column('is_paid', sa.Boolean(), server_default='false'),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_status', sa.String(50), nullable=True),
            sa.Column('last_status_checked_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('link_url', sa.Text(), nullable=True),
            sa.Column('link_page_url', sa.Text(), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('callback_payload', sa.JSON(), nullable=True),
            sa.Column('payment_id', sa.String(255), nullable=True, index=True),
            sa.Column('payment_status', sa.String(50), nullable=True),
            sa.Column('payment_method', sa.String(50), nullable=True),
            sa.Column('balance_amount', sa.String(50), nullable=True),
            sa.Column('balance_currency', sa.String(10), nullable=True),
            sa.Column('payer_account', sa.String(255), nullable=True),
            sa.Column('ttl', sa.Integer(), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      onupdate=sa.func.now()),
        )

    if not _table_exists('wata_payments'):
        op.create_table(
            'wata_payments',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
            sa.Column('payment_link_id', sa.String(64), unique=True, nullable=False, index=True),
            sa.Column('order_id', sa.String(255), nullable=True, index=True),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('type', sa.String(50), nullable=True),
            sa.Column('status', sa.String(50), nullable=False, server_default='Opened'),
            sa.Column('is_paid', sa.Boolean(), server_default='false'),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_status', sa.String(50), nullable=True),
            sa.Column('terminal_public_id', sa.String(64), nullable=True),
            sa.Column('url', sa.Text(), nullable=True),
            sa.Column('success_redirect_url', sa.Text(), nullable=True),
            sa.Column('fail_redirect_url', sa.Text(), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('callback_payload', sa.JSON(), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      onupdate=sa.func.now()),
        )

    if not _table_exists('freekassa_payments'):
        op.create_table(
            'freekassa_payments',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
            sa.Column('order_id', sa.String(64), unique=True, nullable=False, index=True),
            sa.Column('freekassa_order_id', sa.String(64), unique=True, nullable=True, index=True),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
            sa.Column('is_paid', sa.Boolean(), server_default='false'),
            sa.Column('payment_url', sa.Text(), nullable=True),
            sa.Column('payment_system_id', sa.Integer(), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('callback_payload', sa.JSON(), nullable=True),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      onupdate=sa.func.now()),
        )

    if not _table_exists('kassa_ai_payments'):
        op.create_table(
            'kassa_ai_payments',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
            sa.Column('order_id', sa.String(64), unique=True, nullable=False, index=True),
            sa.Column('kassa_ai_order_id', sa.String(64), unique=True, nullable=True, index=True),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
            sa.Column('is_paid', sa.Boolean(), server_default='false'),
            sa.Column('payment_url', sa.Text(), nullable=True),
            sa.Column('payment_system_id', sa.Integer(), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('callback_payload', sa.JSON(), nullable=True),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      onupdate=sa.func.now()),
        )

    if not _table_exists('partner_applications'):
        op.create_table(
            'partner_applications',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('company_name', sa.String(255), nullable=True),
            sa.Column('website_url', sa.String(500), nullable=True),
            sa.Column('telegram_channel', sa.String(255), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('expected_monthly_referrals', sa.Integer(), nullable=True),
            sa.Column('desired_commission_percent', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
            sa.Column('admin_comment', sa.Text(), nullable=True),
            sa.Column('approved_commission_percent', sa.Integer(), nullable=True),
            sa.Column('processed_by', sa.Integer(),
                      sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      onupdate=sa.func.now()),
        )


def downgrade() -> None:
    pass
