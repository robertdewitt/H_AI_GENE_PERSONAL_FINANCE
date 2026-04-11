"""Tests for the financial truth engine — v1 scope.

Covers: event classification, balance dispatch metadata,
reconciliation invariant math, decomposition sums,
and data-quality blocker ordering.
"""
import pytest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.enums import (
    BalanceTruthSource,
    ClassificationProvenance,
    EconomicEventType,
    FeeTreatment,
    FxTreatmentMode,
    PaymentComponent,
    ReconciliationGroupType,
    ReconciliationStatus,
)
from app.models.reconciliation import ReconciliationGroup, ReconciliationMember
from app.models.payment_decomposition import PaymentDecomposition
from app.models.transaction import Transaction


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ── Helpers ──────────────────────────────────────────────────────────


def _make_account(db, name="Test", acct_type=AccountType.CHECKING, **kw):
    kw.setdefault("is_asset", True)
    acct = Account(name=name, account_type=acct_type, currency="USD", **kw)
    db.add(acct)
    db.flush()
    return acct


def _make_txn(db, account_id, amount, desc="txn", date=None, **kw):
    txn = Transaction(
        account_id=account_id,
        amount=amount,
        description=desc,
        date=date or datetime(2025, 6, 15),
        original_currency="USD",
        **kw,
    )
    db.add(txn)
    db.flush()
    return txn


# ── Event classifier ────────────────────────────────────────────────


class TestEventClassifier:
    def test_checking_income(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        txn = _make_txn(db, acct.id, 1500.0, desc="Payroll deposit")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.EXTERNAL_INCOME

    def test_checking_expense(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        txn = _make_txn(db, acct.id, -42.50, desc="Coffee shop")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.LIFESTYLE_EXPENSE

    def test_credit_card_purchase(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CREDIT_CARD, is_asset=False)
        txn = _make_txn(db, acct.id, -80.0, desc="Amazon")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.CARD_PURCHASE

    def test_credit_card_payment(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CREDIT_CARD, is_asset=False)
        txn = _make_txn(db, acct.id, 500.0, desc="Payment thank you")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.CARD_PAYMENT_SETTLEMENT

    def test_internal_transfer(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.SAVINGS)
        txn = _make_txn(db, acct.id, -200.0, desc="Transfer out", is_transfer=True)
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.INTERNAL_TRANSFER

    def test_investment_flow(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.BROKERAGE)
        txn = _make_txn(db, acct.id, 5000.0, desc="Contribution")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.INVESTMENT_FLOW

    def test_mortgage_payment(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.MORTGAGE, is_asset=False)
        txn = _make_txn(db, acct.id, 1200.0, desc="Monthly payment")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.LIABILITY_PAYMENT

    def test_fee_keyword(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        txn = _make_txn(db, acct.id, -35.0, desc="Overdraft fee")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.FEE

    def test_classify_batch(self, db):
        from app.services.event_classifier import classify_batch
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        t1 = _make_txn(db, acct.id, 100.0, desc="Deposit")
        t2 = _make_txn(db, acct.id, -50.0, desc="Groceries")
        db.commit()

        count = classify_batch(db, transaction_ids=[t1.id, t2.id])
        db.commit()
        assert count == 2

        db.refresh(t1)
        db.refresh(t2)
        assert t1.event_type == EconomicEventType.EXTERNAL_INCOME.value
        assert t2.event_type == EconomicEventType.LIFESTYLE_EXPENSE.value
        assert t1.classification_provenance == ClassificationProvenance.INFERRED.value


# ── Balance dispatch ────────────────────────────────────────────────


class TestBalanceDispatch:
    def test_transaction_sum_default(self, db):
        from app.services.account_service import get_account_balance_rich
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        _make_txn(db, acct.id, 1000.0)
        _make_txn(db, acct.id, -250.0)
        db.commit()

        result = get_account_balance_rich(db, acct.id)
        assert result.value == 750.0
        assert result.balance_source_used == BalanceTruthSource.TRANSACTION_SUM.value
        assert result.balance_as_of is not None

    def test_statement_source(self, db):
        from app.services.account_service import get_account_balance_rich
        recent = datetime.now()
        acct = _make_account(
            db,
            acct_type=AccountType.CHECKING,
            balance_truth_source=BalanceTruthSource.LATEST_STATEMENT.value,
            statement_balance=5000.0,
            statement_balance_as_of=recent,
        )
        db.commit()

        result = get_account_balance_rich(db, acct.id)
        assert result.value == 5000.0
        assert result.balance_source_used == BalanceTruthSource.LATEST_STATEMENT.value
        assert not result.balance_stale

    def test_manual_mark_stale(self, db):
        from app.services.account_service import get_account_balance_rich
        acct = _make_account(
            db,
            acct_type=AccountType.REAL_ESTATE,
            balance_truth_source=BalanceTruthSource.MANUAL_MARK.value,
            current_value=350000.0,
            value_as_of_date=datetime(2020, 1, 1),
        )
        db.commit()

        result = get_account_balance_rich(db, acct.id)
        assert result.value == 350000.0
        assert result.balance_stale is True
        assert result.balance_confidence < 0.5

    def test_liability_balance(self, db):
        from app.services.account_service import get_account_balance_rich
        acct = _make_account(
            db,
            acct_type=AccountType.MORTGAGE,
            is_asset=False,
            balance_truth_source=BalanceTruthSource.LIABILITY_BALANCE.value,
            statement_balance=180000.0,
            statement_balance_as_of=datetime(2025, 5, 1),
        )
        db.commit()

        result = get_account_balance_rich(db, acct.id)
        assert result.value == 180000.0
        assert result.balance_source_used == BalanceTruthSource.LIABILITY_BALANCE.value

    def test_backward_compat_float(self, db):
        from app.services.account_service import get_account_balance
        acct = _make_account(db)
        _make_txn(db, acct.id, 100.0)
        db.commit()
        assert isinstance(get_account_balance(db, acct.id), float)


# ── Reconciliation invariants ───────────────────────────────────────


class TestReconciliationInvariants:
    def test_balanced_group(self, db):
        from app.services.reconciliation_invariants import validate_group
        acct1 = _make_account(db, name="Checking")
        acct2 = _make_account(db, name="Savings")
        t1 = _make_txn(db, acct1.id, -500.0)
        t2 = _make_txn(db, acct2.id, 500.0)

        group = ReconciliationGroup(
            group_type=ReconciliationGroupType.TRANSFER.value,
            status=ReconciliationStatus.SUGGESTED.value,
            base_currency="USD",
            tolerance_base=0.01,
        )
        db.add(group)
        db.flush()

        m1 = ReconciliationMember(
            group_id=group.id, transaction_id=t1.id,
            allocated_amount_native=-500.0, allocated_currency="USD",
            allocated_amount_base=-500.0,
        )
        m2 = ReconciliationMember(
            group_id=group.id, transaction_id=t2.id,
            allocated_amount_native=500.0, allocated_currency="USD",
            allocated_amount_base=500.0,
        )
        db.add_all([m1, m2])
        db.flush()
        db.commit()

        db.refresh(group)
        result = validate_group(db, group)
        assert result.balanced is True
        assert abs(result.net_base) < 0.01

    def test_unbalanced_group(self, db):
        from app.services.reconciliation_invariants import validate_group
        acct = _make_account(db)
        t1 = _make_txn(db, acct.id, -500.0)
        t2 = _make_txn(db, acct.id, 490.0)

        group = ReconciliationGroup(
            group_type=ReconciliationGroupType.TRANSFER.value,
            base_currency="USD", tolerance_base=0.01,
        )
        db.add(group)
        db.flush()

        m1 = ReconciliationMember(
            group_id=group.id, transaction_id=t1.id,
            allocated_amount_native=-500.0, allocated_currency="USD",
            allocated_amount_base=-500.0,
        )
        m2 = ReconciliationMember(
            group_id=group.id, transaction_id=t2.id,
            allocated_amount_native=490.0, allocated_currency="USD",
            allocated_amount_base=490.0,
        )
        db.add_all([m1, m2])
        db.flush()
        db.commit()

        db.refresh(group)
        result = validate_group(db, group)
        assert result.balanced is False
        assert any("exceeds tolerance" in w for w in result.warnings)

    def test_fee_excluded(self, db):
        from app.services.reconciliation_invariants import validate_group
        acct = _make_account(db)
        t1 = _make_txn(db, acct.id, -500.0)
        t2 = _make_txn(db, acct.id, 500.0)
        t_fee = _make_txn(db, acct.id, -5.0, desc="Wire fee")

        group = ReconciliationGroup(
            group_type=ReconciliationGroupType.TRANSFER.value,
            base_currency="USD", tolerance_base=0.01,
            fee_treatment=FeeTreatment.EXCLUDE_FROM_NET.value,
        )
        db.add(group)
        db.flush()

        db.add_all([
            ReconciliationMember(
                group_id=group.id, transaction_id=t1.id,
                allocated_amount_native=-500.0, allocated_currency="USD",
                allocated_amount_base=-500.0,
            ),
            ReconciliationMember(
                group_id=group.id, transaction_id=t2.id,
                allocated_amount_native=500.0, allocated_currency="USD",
                allocated_amount_base=500.0,
            ),
            ReconciliationMember(
                group_id=group.id, transaction_id=t_fee.id,
                allocated_amount_native=-5.0, allocated_currency="USD",
                allocated_amount_base=-5.0, is_fee_leg=True,
            ),
        ])
        db.flush()
        db.commit()

        db.refresh(group)
        result = validate_group(db, group)
        assert result.balanced is True


# ── Payment decomposition ───────────────────────────────────────────


class TestPaymentDecomposition:
    def test_valid_decomposition(self, db):
        from app.services.payment_decomposition_service import (
            add_component, validate_decomposition,
        )
        acct = _make_account(db, acct_type=AccountType.MORTGAGE, is_asset=False)
        txn = _make_txn(db, acct.id, -1200.0, desc="Mortgage payment")
        db.commit()

        add_component(db, txn.id, PaymentComponent.PRINCIPAL, -800.0, "USD")
        add_component(db, txn.id, PaymentComponent.INTEREST, -350.0, "USD")
        add_component(db, txn.id, PaymentComponent.ESCROW, -50.0, "USD")
        db.commit()

        result = validate_decomposition(db, txn.id)
        assert result.valid is True
        assert abs(result.residual) <= result.tolerance

    def test_invalid_decomposition(self, db):
        from app.services.payment_decomposition_service import (
            add_component, validate_decomposition,
        )
        acct = _make_account(db, acct_type=AccountType.MORTGAGE, is_asset=False)
        txn = _make_txn(db, acct.id, -1200.0, desc="Mortgage payment")
        db.commit()

        add_component(db, txn.id, PaymentComponent.PRINCIPAL, -800.0, "USD")
        db.commit()

        result = validate_decomposition(db, txn.id)
        assert result.valid is False
        assert any("differs" in w for w in result.warnings)


# ── Data quality ────────────────────────────────────────────────────


class TestDataQuality:
    def test_blockers_before_score(self, db):
        from app.services.data_quality import assess_quality
        report = assess_quality(db)
        # No transactions => blocker
        assert len(report.blockers) > 0
        assert report.close_readiness_score < 100

    def test_uncategorized_warning(self, db):
        from app.services.data_quality import assess_quality
        acct = _make_account(db)
        for i in range(10):
            _make_txn(db, acct.id, -10.0 * (i + 1), desc=f"txn {i}")
        db.commit()

        report = assess_quality(db)
        # All 10 uncategorized out of 10 total => blocker (>50%)
        has_uncat = any("uncategorized" in b for b in report.blockers)
        assert has_uncat

    def test_clean_ledger(self, db):
        from app.services.data_quality import assess_quality
        from app.models.category import Category

        cat = Category(name="Food", category_type="expense")
        db.add(cat)
        db.flush()

        acct = _make_account(db)
        for i in range(5):
            _make_txn(
                db, acct.id, -20.0, desc=f"Lunch {i}",
                category_id=cat.id,
                event_type=EconomicEventType.LIFESTYLE_EXPENSE.value,
            )
        db.commit()

        report = assess_quality(db)
        assert report.close_readiness_score > 0
        assert len(report.blockers) == 0
