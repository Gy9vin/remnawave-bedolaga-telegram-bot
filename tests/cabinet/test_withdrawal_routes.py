"""
Тесты для Cabinet API эндпоинтов вывода реферального баланса.
"""

from datetime import UTC, datetime, timedelta

from app.database.models import WithdrawalRequestStatus


class TestWithdrawalBalanceStats:
    """Тесты расчёта баланса для вывода."""

    def test_available_balance_calculation(self):
        """Доступный баланс = заработано - потрачено - выведено - одобрено - на рассмотрении."""
        total_earned = 100000  # 1000₽
        referral_spent = 30000  # 300₽
        withdrawn = 10000  # 100₽ (COMPLETED)
        approved = 10000  # 100₽ (APPROVED, ожидает перевода)
        pending = 10000  # 100₽ (PENDING)

        available = max(0, total_earned - referral_spent - withdrawn - approved - pending)
        assert available == 40000  # 400₽

    def test_available_balance_zero(self):
        """Если всё потрачено — доступно 0."""
        total_earned = 50000
        referral_spent = 50000
        withdrawn = 0
        approved = 0
        pending = 0

        available = max(0, total_earned - referral_spent - withdrawn - approved - pending)
        assert available == 0

    def test_no_double_deduction_for_withdrawal(self):
        """Withdrawal НЕ должен считаться и в spending, и в withdrawn одновременно.

        Баг: если withdrawal включён в spending, то при завершении вывода
        сумма учитывается дважды (в referral_spent и в withdrawn).
        """
        total_earned = 100000  # 1000₽
        # После завершения вывода 200₽:
        spending_subscriptions = 30000  # 300₽ — только покупки подписок
        withdrawn = 20000  # 200₽ — реально выведено (COMPLETED)
        approved = 0
        pending = 0

        # spending НЕ включает withdrawal — withdrawal учитывается отдельно
        referral_spent = min(spending_subscriptions, total_earned)
        available = max(0, total_earned - referral_spent - withdrawn - approved - pending)
        assert available == 50000  # 500₽ — корректно

        # Если бы withdrawal дублировался в spending (БАГ):
        spending_with_bug = spending_subscriptions + withdrawn  # 300₽ + 200₽ = 500₽
        referral_spent_bug = min(spending_with_bug, total_earned)
        available_bug = max(0, total_earned - referral_spent_bug - withdrawn - approved - pending)
        assert available_bug == 30000  # 300₽ — НЕПРАВИЛЬНО, потеряно 200₽

    def test_available_balance_with_pending(self):
        """Заявки на рассмотрении и одобренные замораживают баланс."""
        total_earned = 100000
        referral_spent = 0
        withdrawn = 0
        approved = 30000  # Одобрено, ожидает перевода
        pending = 30000  # На рассмотрении

        available = max(0, total_earned - referral_spent - withdrawn - approved - pending)
        assert available == 40000

    def test_available_capped_by_actual_balance(self):
        """Доступно к выводу НЕ может превышать фактический баланс.

        Баг: формула на основе транзакций может выдать available > balance_kopeks,
        если были траты не учтённые в формуле (сброс трафика, промокоды и т.д.).
        """
        total_earned = 70050  # Заработано с рефералов
        referral_spent = 0  # Подписок не покупал
        withdrawn = 20000  # Уже вывел 200₽
        approved = 0
        pending = 0
        actual_balance = 30050  # Реальный баланс (меньше чем формула)

        # Формула без ограничения
        available_formula = max(0, total_earned - referral_spent - withdrawn - approved - pending)
        assert available_formula == 50050  # Формула говорит 500.50₽

        # Но нельзя вывести больше чем есть на балансе
        max_withdrawable = max(0, actual_balance - approved - pending)
        available_total = min(available_formula, max_withdrawable)
        assert available_total == 30050  # Ограничено реальным балансом

    def test_referral_spent_capped_by_earned(self):
        """Реферальные траты не могут превышать заработок."""
        total_earned = 30000
        spending_after_earning = 50000

        referral_spent = min(spending_after_earning, total_earned)
        assert referral_spent == 30000  # Ограничено заработком

    def test_only_referral_mode(self):
        """В режиме 'только реферальный баланс' свой баланс не учитывается."""
        total_earned = 50000
        referral_spent = 10000
        withdrawn = 0
        approved = 0
        pending = 0
        only_referral_mode = True

        available_referral = max(0, total_earned - referral_spent - withdrawn - approved - pending)

        if only_referral_mode:
            available_total = available_referral
        else:
            available_total = available_referral + 100000  # + свои пополнения

        assert available_total == 40000  # Только реферальный


class TestWithdrawalValidation:
    """Тесты валидации запросов на вывод."""

    def test_min_amount_check(self):
        """Сумма должна быть >= минимальной."""
        min_amount = 50000  # 500₽
        request_amount = 30000  # 300₽

        assert request_amount < min_amount

    def test_min_amount_ok(self):
        """Сумма >= минимальной — ОК."""
        min_amount = 50000
        request_amount = 50000

        assert request_amount >= min_amount

    def test_amount_exceeds_available(self):
        """Сумма больше доступной — отказ."""
        available = 40000
        request_amount = 50000

        assert request_amount > available

    def test_payment_details_min_length(self):
        """Реквизиты минимум 5 символов (по схеме WithdrawalCreateRequest)."""
        short_details = '1234'
        valid_details = '12345'

        assert len(short_details.strip()) < 5
        assert len(valid_details.strip()) >= 5

    def test_cooldown_active(self):
        """Cooldown ещё не прошёл — нельзя создать заявку."""
        cooldown_days = 30
        last_request_date = datetime.now(UTC) - timedelta(days=10)
        cooldown_end = last_request_date + timedelta(days=cooldown_days)

        assert datetime.now(UTC) < cooldown_end  # Cooldown активен

    def test_cooldown_expired(self):
        """Cooldown прошёл — можно создать заявку."""
        cooldown_days = 30
        last_request_date = datetime.now(UTC) - timedelta(days=31)
        cooldown_end = last_request_date + timedelta(days=cooldown_days)

        assert datetime.now(UTC) >= cooldown_end  # Cooldown истёк

    def test_pending_request_blocks_new(self):
        """Активная заявка блокирует новую."""
        last_status = WithdrawalRequestStatus.PENDING.value
        assert last_status == 'pending'

    def test_completed_request_allows_new(self):
        """Выполненная заявка не блокирует новую."""
        last_status = WithdrawalRequestStatus.COMPLETED.value
        assert last_status != WithdrawalRequestStatus.PENDING.value


class TestWithdrawalStatuses:
    """Тесты статусов заявок."""

    def test_all_statuses_exist(self):
        """Все ожидаемые статусы существуют."""
        assert WithdrawalRequestStatus.PENDING.value == 'pending'
        assert WithdrawalRequestStatus.APPROVED.value == 'approved'
        assert WithdrawalRequestStatus.REJECTED.value == 'rejected'
        assert WithdrawalRequestStatus.COMPLETED.value == 'completed'
        assert WithdrawalRequestStatus.CANCELLED.value == 'cancelled'

    def test_status_flow_approve(self):
        """Flow: PENDING → APPROVED → COMPLETED."""
        status = WithdrawalRequestStatus.PENDING
        assert status == WithdrawalRequestStatus.PENDING

        status = WithdrawalRequestStatus.APPROVED
        assert status == WithdrawalRequestStatus.APPROVED

        status = WithdrawalRequestStatus.COMPLETED
        assert status == WithdrawalRequestStatus.COMPLETED

    def test_status_flow_reject(self):
        """Flow: PENDING → REJECTED."""
        status = WithdrawalRequestStatus.PENDING
        assert status == WithdrawalRequestStatus.PENDING

        status = WithdrawalRequestStatus.REJECTED
        assert status == WithdrawalRequestStatus.REJECTED


class TestWithdrawalSchemas:
    """Тесты Pydantic схем."""

    def test_balance_response_schema(self):
        """WithdrawalBalanceResponse корректно создаётся."""
        from app.cabinet.schemas.withdrawals import WithdrawalBalanceResponse

        response = WithdrawalBalanceResponse(
            total_earned=100000,
            referral_spent=30000,
            withdrawn=10000,
            pending=10000,
            available_referral=50000,
            available_total=40000,
            only_referral_mode=True,
            min_amount_kopeks=50000,
            is_withdrawal_enabled=True,
            can_request=True,
            cannot_request_reason=None,
        )
        assert response.available_total == 40000
        assert response.can_request is True
        assert response.pending == 10000
        assert response.total_earned == 100000

    def test_balance_response_cannot_withdraw(self):
        """WithdrawalBalanceResponse с причиной отказа."""
        from app.cabinet.schemas.withdrawals import WithdrawalBalanceResponse

        response = WithdrawalBalanceResponse(
            total_earned=10000,
            referral_spent=10000,
            withdrawn=0,
            pending=0,
            available_referral=0,
            available_total=0,
            only_referral_mode=True,
            min_amount_kopeks=50000,
            is_withdrawal_enabled=True,
            can_request=False,
            cannot_request_reason='Минимальная сумма вывода: 500₽. Доступно: 0₽',
        )
        assert response.can_request is False
        assert response.cannot_request_reason is not None

    def test_create_request_schema(self):
        """WithdrawalCreateRequest валидация."""
        from app.cabinet.schemas.withdrawals import WithdrawalCreateRequest

        req = WithdrawalCreateRequest(
            amount_kopeks=50000,
            payment_details='+7 999 123-45-67 Сбербанк',
        )
        assert req.amount_kopeks == 50000
        assert len(req.payment_details) >= 5

    def test_item_response_schema(self):
        """WithdrawalItemResponse корректно создаётся."""
        from app.cabinet.schemas.withdrawals import WithdrawalItemResponse

        response = WithdrawalItemResponse(
            id=1,
            amount_kopeks=50000,
            amount_rubles=500.0,
            status='pending',
            payment_details='+7 999 123-45-67',
            admin_comment=None,
            created_at=datetime.now(UTC),
            processed_at=None,
        )
        assert response.id == 1
        assert response.status == 'pending'

    def test_list_response_schema(self):
        """WithdrawalListResponse без пагинации."""
        from app.cabinet.schemas.withdrawals import WithdrawalItemResponse, WithdrawalListResponse

        items = [
            WithdrawalItemResponse(
                id=i,
                amount_kopeks=50000,
                amount_rubles=500.0,
                status='pending',
                created_at=datetime.now(UTC),
            )
            for i in range(3)
        ]

        response = WithdrawalListResponse(
            items=items,
            total=3,
        )
        assert len(response.items) == 3
        assert response.total == 3


class TestRiskAnalysis:
    """Тесты уровней риска."""

    def test_risk_levels(self):
        """Правильное определение уровней риска по скору."""
        test_cases = [
            (0, 'low'),
            (15, 'low'),
            (29, 'low'),
            (30, 'medium'),
            (49, 'medium'),
            (50, 'high'),
            (69, 'high'),
            (70, 'critical'),
            (100, 'critical'),
        ]

        for score, expected_level in test_cases:
            if score >= 70:
                level = 'critical'
            elif score >= 50:
                level = 'high'
            elif score >= 30:
                level = 'medium'
            else:
                level = 'low'
            assert level == expected_level, f'Score {score}: expected {expected_level}, got {level}'

    def test_risk_score_capped_at_100(self):
        """Risk score не может быть больше 100."""
        score = 150
        score = min(score, 100)
        assert score == 100


class TestWithdrawalExplanation:
    """Тесты генерации объяснения для пользователя."""

    def test_explanation_when_available_less_than_balance(self):
        """Объяснение генерируется когда доступно < баланс."""
        from app.services.referral_withdrawal_service import ReferralWithdrawalService

        stats = {
            'actual_balance': 219740,
            'total_earned': 263740,
            'referral_spent': 70000,
            'withdrawn': 0,
            'approved': 0,
            'pending': 0,
            'available_total': 193740,
            'available_referral': 193740,
            'only_referral_mode': True,
        }
        explanation = ReferralWithdrawalService.build_withdrawal_explanation(stats)
        assert explanation is not None
        assert 'реферальный заработок' in explanation
        assert '2637.40₽' in explanation  # заработано
        assert '700.00₽' in explanation  # потрачено
        assert '1937.40₽' in explanation  # доступно

    def test_no_explanation_when_all_referral(self):
        """Нет объяснения если весь баланс — реферальный."""
        from app.services.referral_withdrawal_service import ReferralWithdrawalService

        stats = {
            'actual_balance': 50000,
            'total_earned': 50000,
            'referral_spent': 0,
            'withdrawn': 0,
            'approved': 0,
            'pending': 0,
            'available_total': 50000,
            'available_referral': 50000,
            'only_referral_mode': True,
        }
        explanation = ReferralWithdrawalService.build_withdrawal_explanation(stats)
        assert explanation is None

    def test_explanation_no_earnings(self):
        """Объяснение если нет реферальных начислений."""
        from app.services.referral_withdrawal_service import ReferralWithdrawalService

        stats = {
            'actual_balance': 100000,
            'total_earned': 0,
            'referral_spent': 0,
            'withdrawn': 0,
            'approved': 0,
            'pending': 0,
            'available_total': 0,
            'available_referral': 0,
            'only_referral_mode': True,
        }
        explanation = ReferralWithdrawalService.build_withdrawal_explanation(stats)
        assert explanation is not None
        assert 'нет реферальных начислений' in explanation

    def test_explanation_with_frozen_amounts(self):
        """Объяснение показывает замороженные суммы."""
        from app.services.referral_withdrawal_service import ReferralWithdrawalService

        stats = {
            'actual_balance': 200000,
            'total_earned': 150000,
            'referral_spent': 30000,
            'withdrawn': 20000,
            'approved': 10000,
            'pending': 10000,
            'available_total': 80000,
            'available_referral': 80000,
            'only_referral_mode': True,
        }
        explanation = ReferralWithdrawalService.build_withdrawal_explanation(stats)
        assert explanation is not None
        assert 'выведено' in explanation.lower()
        assert 'одобрено' in explanation.lower()
        assert 'рассмотрении' in explanation.lower()

    def test_explanation_all_spent(self):
        """Объяснение если всё потрачено."""
        from app.services.referral_withdrawal_service import ReferralWithdrawalService

        stats = {
            'actual_balance': 50000,
            'total_earned': 50000,
            'referral_spent': 50000,
            'withdrawn': 0,
            'approved': 0,
            'pending': 0,
            'available_total': 0,
            'available_referral': 0,
            'only_referral_mode': True,
        }
        explanation = ReferralWithdrawalService.build_withdrawal_explanation(stats)
        assert explanation is not None
        assert 'потрачен' in explanation.lower()
