"""Custom tables and columns (consolidated from old 0041-0045 + upstream 0041-0045)

Adds payment tables, partner_applications, show_in_gift, blacklist_exceptions,
AI forum columns, riopay nullable user_id, severpay_payments.
Also applies upstream 0041-0045 content (indexes, retry_count, receipt_uuid,
payment_method data migration) that was skipped on the production server.
All operations are idempotent (IF NOT EXISTS checks).

Revision ID: 0047
Revises: 0046
Create Date: 2026-03-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = '0047'
down_revision: Union[str, None] = '0046'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return False
    cols = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in cols


def _fk_exists(table_name: str, fk_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    fks = insp.get_foreign_keys(table_name)
    return any(fk.get('name') == fk_name for fk in fks)


def upgrade() -> None:
    # ── Payment tables ──────────────────────────────────────────────────────

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
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
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
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
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
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
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
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
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
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
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
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
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
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
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
            sa.Column('processed_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )

    # ── tariffs.show_in_gift ─────────────────────────────────────────────────

    if not _column_exists('tariffs', 'show_in_gift'):
        op.add_column(
            'tariffs',
            sa.Column('show_in_gift', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        )

    # ── blacklist_exceptions ─────────────────────────────────────────────────

    if not _table_exists('blacklist_exceptions'):
        op.create_table(
            'blacklist_exceptions',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('telegram_id', sa.BigInteger(), nullable=False, unique=True, index=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    # ── AI forum columns ─────────────────────────────────────────────────────

    if not _column_exists('tickets', 'forum_topic_id'):
        op.add_column('tickets', sa.Column('forum_topic_id', sa.Integer(), nullable=True))

    if not _column_exists('tickets', 'forum_control_msg_id'):
        op.add_column('tickets', sa.Column('forum_control_msg_id', sa.Integer(), nullable=True))

    if not _column_exists('tickets', 'ai_enabled'):
        op.add_column(
            'tickets',
            sa.Column('ai_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        )

    if not _column_exists('tickets', 'operator_telegram_id'):
        op.add_column('tickets', sa.Column('operator_telegram_id', sa.BigInteger(), nullable=True))

    if not _column_exists('ticket_messages', 'is_ai_response'):
        op.add_column(
            'ticket_messages',
            sa.Column('is_ai_response', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )

    # ── riopay_payments: make user_id nullable ───────────────────────────────

    if _table_exists('riopay_payments') and not _column_exists('riopay_payments', 'user_id') is False:
        conn = op.get_bind()
        insp = inspect(conn)
        cols = {c['name']: c for c in insp.get_columns('riopay_payments')}
        if 'user_id' in cols and not cols['user_id']['nullable']:
            op.alter_column('riopay_payments', 'user_id', existing_type=sa.Integer(), nullable=True)
            if _fk_exists('riopay_payments', 'riopay_payments_user_id_fkey'):
                op.drop_constraint('riopay_payments_user_id_fkey', 'riopay_payments', type_='foreignkey')
                op.create_foreign_key(
                    None, 'riopay_payments', 'users', ['user_id'], ['id'], ondelete='SET NULL'
                )

    if not _table_exists('severpay_payments'):
        op.create_table(
            'severpay_payments',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True),
            sa.Column('order_id', sa.String(64), unique=True, nullable=False, index=True),
            sa.Column('severpay_id', sa.String(64), unique=True, nullable=True, index=True),
            sa.Column('severpay_uid', sa.String(64), unique=True, nullable=True, index=True),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
            sa.Column('is_paid', sa.Boolean(), server_default=sa.text('false')),
            sa.Column('payment_url', sa.Text(), nullable=True),
            sa.Column('payment_method', sa.String(32), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('callback_payload', sa.JSON(), nullable=True),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
        )

    # ── From upstream 0041: performance indexes ──────────────────────────────

    conn = op.get_bind()
    dialect = conn.dialect.name

    def _index_exists(index_name: str) -> bool:
        if dialect == 'postgresql':
            result = conn.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
                {'n': index_name},
            )
        else:
            result = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='index' AND name = :n"),
                {'n': index_name},
            )
        return result.fetchone() is not None

    # CONCURRENTLY нельзя внутри транзакции Alembic — используем обычный CREATE INDEX
    if not _index_exists('ix_campaign_reg_user_created'):
        if _table_exists('campaign_registrations'):
            try:
                op.create_index(
                    'ix_campaign_reg_user_created',
                    'campaign_registrations',
                    ['user_id', 'created_at'],
                )
            except Exception:
                pass

    if not _index_exists('ix_transactions_user_type_completed_amount'):
        if _table_exists('transactions'):
            try:
                op.create_index(
                    'ix_transactions_user_type_completed_amount',
                    'transactions',
                    ['user_id', 'type', 'is_completed', 'amount_kopeks'],
                )
            except Exception:
                pass

    # ── From upstream 0042: retry_count + metadata indexes ───────────────────

    if not _column_exists('guest_purchases', 'retry_count'):
        op.add_column(
            'guest_purchases',
            sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        )

    for idx_name, tbl, cols in [
        ('ix_yookassa_payments_metadata', 'yookassa_payments', ['metadata_json']),
        ('ix_cryptobot_payments_payload', 'cryptobot_payments', ['payload']),
        ('ix_heleket_payments_metadata', 'heleket_payments', ['metadata_json']),
        ('ix_mulenpay_payments_metadata', 'mulenpay_payments', ['metadata_json']),
        ('ix_pal24_payments_metadata', 'pal24_payments', ['metadata_json']),
        ('ix_wata_payments_metadata', 'wata_payments', ['metadata_json']),
        ('ix_freekassa_payments_metadata', 'freekassa_payments', ['metadata_json']),
        ('ix_kassa_ai_payments_metadata', 'kassa_ai_payments', ['metadata_json']),
    ]:
        if _table_exists(tbl) and not _index_exists(idx_name):
            try:
                op.create_index(idx_name, tbl, cols)
            except Exception:
                pass  # index may already exist under a different name

    # ── From upstream 0043: RBAC + email indexes ─────────────────────────────

    for idx_name, tbl, cols in [
        ('ix_user_roles_role_id', 'user_roles', ['role_id']),
        ('ix_access_policies_role_id', 'access_policies', ['role_id']),
    ]:
        if _table_exists(tbl) and not _index_exists(idx_name):
            try:
                op.create_index(idx_name, tbl, cols)
            except Exception:
                pass

    # ix_users_email_lower — функциональный индекс, только PostgreSQL
    if _table_exists('users') and not _index_exists('ix_users_email_lower'):
        if dialect == 'postgresql':
            try:
                conn.execute(
                    text('CREATE INDEX IF NOT EXISTS ix_users_email_lower ON users (lower(email))')
                )
            except Exception:
                pass
        # SQLite не поддерживает функциональные индексы — пропускаем

    # ── From upstream 0044: fix null payment_method on manual top-ups ─────────

    conn.execute(
        text(
            "UPDATE transactions SET payment_method = 'manual' "
            "WHERE type = 'deposit' "
            "  AND payment_method IS NULL "
            "  AND is_completed = TRUE "
            "  AND (description IS NULL "
            "       OR (description NOT LIKE '%Stars%' "
            "           AND description NOT LIKE '%YooKassa%' "
            "           AND description NOT LIKE '%CryptoBot%' "
            "           AND description NOT LIKE '%Heleket%' "
            "           AND description NOT LIKE '%MulenPay%' "
            "           AND description NOT LIKE '%Pal24%' "
            "           AND description NOT LIKE '%Platega%' "
            "           AND description NOT LIKE '%WATA%' "
            "           AND description NOT LIKE '%CloudPayments%' "
            "           AND description NOT LIKE '%Freekassa%' "
            "           AND description NOT LIKE '%KassaAI%' "
            "           AND description NOT LIKE '%Tribute%'))"
        )
    )

    # ── From upstream 0045: receipt_uuid + receipt_created_at ────────────────

    if not _column_exists('guest_purchases', 'receipt_uuid'):
        op.add_column(
            'guest_purchases',
            sa.Column('receipt_uuid', sa.String(255), nullable=True),
        )

    if not _column_exists('guest_purchases', 'receipt_created_at'):
        op.add_column(
            'guest_purchases',
            sa.Column('receipt_created_at', sa.DateTime(timezone=True), nullable=True),
        )

    if not _index_exists('ix_guest_purchases_receipt_uuid'):
        if _column_exists('guest_purchases', 'receipt_uuid'):
            try:
                op.create_index(
                    'ix_guest_purchases_receipt_uuid',
                    'guest_purchases',
                    ['receipt_uuid'],
                )
            except Exception:
                pass


def downgrade() -> None:
    pass
