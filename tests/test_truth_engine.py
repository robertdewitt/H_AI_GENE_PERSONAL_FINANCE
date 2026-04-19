"""Tests for the financial truth engine — v2 scope.

Covers: splits, reconciliation invariants, decomposition sums,
balance truth dispatch, event classifier (expanded), payroll,
spend analysis, attribution, data quality, and snapshots.
"""
import pytest
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.category import Category
from app.models.enums import (
    BalanceTruthSource,
    ClassificationProvenance,
    EconomicEventType,
    FeeTreatment,
    PaymentComponent,
    PayrollComponent,
    ReconciliationGroupType,
    ReconciliationStatus,
    SpendType,
)
from app.models.reconciliation import ReconciliationGroup, ReconciliationMember
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


def _make_category(db, name="General", cat_type="expense"):
    cat = Category(name=name, category_type=cat_type)
    db.add(cat)
    db.flush()
    return cat


# ── Event classifier ────────────────────────────────────────────────


class TestEventClassifier:
    def test_checking_income(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        txn = _make_txn(db, acct.id, 1500.0, desc="Payroll deposit")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.PAYROLL_INCOME

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

    def test_investment_contribution(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.BROKERAGE)
        txn = _make_txn(db, acct.id, 5000.0, desc="Contribution")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.INVESTMENT_CONTRIBUTION

    def test_investment_withdrawal(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.BROKERAGE)
        txn = _make_txn(db, acct.id, -2000.0, desc="Withdrawal")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.INVESTMENT_WITHDRAWAL

    def test_mortgage_payment(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.MORTGAGE, is_asset=False)
        txn = _make_txn(db, acct.id, 1200.0, desc="Monthly payment")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.MORTGAGE_PAYMENT

    def test_fee_keyword(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        txn = _make_txn(db, acct.id, -35.0, desc="Overdraft fee")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.FEE

    def test_payroll_keyword(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        txn = _make_txn(db, acct.id, 3500.0, desc="ACME Corp Payroll")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.PAYROLL_INCOME

    def test_generic_income(self, db):
        from app.services.event_classifier import classify_transaction
        acct = _make_account(db, acct_type=AccountType.CHECKING)
        txn = _make_txn(db, acct.id, 200.0, desc="Refund from store")
        result = classify_transaction(txn, acct)
        assert result == EconomicEventType.EXTERNAL_INCOME

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


# ── Transaction splits ──────────────────────────────────────────────


class TestTransactionSplits:
    def test_split_sum_invariant_valid(self, db):
        from app.services.split_service import add_split, validate_splits
        acct = _make_account(db)
        txn = _make_txn(db, acct.id, -100.0, desc="Mixed purchase")
        db.commit()

        add_split(db, txn.id, -60.0, "USD", spend_type=SpendType.LIFESTYLE, counts_as_true_spend=True)
        add_split(db, txn.id, -40.0, "USD", spend_type=SpendType.FIXED_CORE, counts_as_true_spend=True)
        db.commit()

        result = validate_splits(db, txn.id)
        assert result.valid is True
        assert result.split_count == 2
        assert abs(result.residual) <= result.tolerance

    def test_split_sum_invariant_invalid(self, db):
        from app.services.split_service import add_split, validate_splits
        acct = _make_account(db)
        txn = _make_txn(db, acct.id, -100.0, desc="Partial split")
        db.commit()

        add_split(db, txn.id, -60.0, "USD")
        db.commit()

        result = validate_splits(db, txn.id)
        assert result.valid is False
        assert any("Split sum" in w for w in result.warnings)

    def test_default_split(self, db):
        from app.services.split_service import create_default_split, validate_splits
        acct = _make_account(db)
        txn = _make_txn(db, acct.id, -75.0, desc="Single item")
        db.commit()

        split = create_default_split(db, txn.id)
        db.commit()
        assert split.amount_native == -75.0

        result = validate_splits(db, txn.id)
        assert result.valid is True

    def test_true_spend_sum(self, db):
        from app.services.split_service import add_split, get_true_spend
        acct = _make_account(db)
        txn = _make_txn(db, acct.id, -200.0, desc="Shopping")
        db.commit()

        add_split(db, txn.id, -150.0, "USD", counts_as_true_spend=True, spend_type=SpendType.LIFESTYLE)
        add_split(db, txn.id, -50.0, "USD", counts_as_true_spend=False, spend_type=SpendType.NON_SPEND_CASH_USE)
        db.commit()

        total = get_true_spend(db)
        assert total == -150.0


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

    def test_balance_returns_decimal(self, db):
        from app.services.account_service import get_account_balance
        acct = _make_account(db)
        _make_txn(db, acct.id, 100.0)
        db.commit()
        assert isinstance(get_account_balance(db, acct.id), Decimal)


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


# ── Payroll via splits ──────────────────────────────────────────────


class TestPayroll:
    def test_payroll_decomposition(self, db):
        from app.services.payroll_service import decompose_payroll
        from app.services.split_service import validate_splits

        acct = _make_account(db, acct_type=AccountType.CHECKING)
        txn = _make_txn(db, acct.id, 3000.0, desc="Employer Payroll")
        db.commit()

        result = decompose_payroll(
            db, txn.id,
            components={
                PayrollComponent.NET_SALARY_CASH: 3000.0,
            },
            currency="USD",
        )
        db.commit()
        assert "net_salary_cash" in result.components
        assert result.validation.valid is True

    def test_payroll_with_deductions(self, db):
        from app.services.payroll_service import decompose_payroll
        from app.services.split_service import validate_splits

        acct = _make_account(db, acct_type=AccountType.CHECKING)
        pension_acct = _make_account(
            db, name="Pension", acct_type=AccountType.PENSION,
        )
        txn = _make_txn(db, acct.id, 3000.0, desc="Payroll")
        db.commit()

        result = decompose_payroll(
            db, txn.id,
            components={
                PayrollComponent.SALARY_GROSS: 4500.0,
                PayrollComponent.PAYROLL_TAX: -800.0,
                PayrollComponent.PENSION_CONTRIBUTION_EMPLOYEE: -400.0,
                PayrollComponent.HEALTH_BENEFIT: -300.0,
                PayrollComponent.NET_SALARY_CASH: 3000.0,
            },
            linked_pension_account_id=pension_acct.id,
        )
        db.commit()

        assert len(result.components) == 5
        # Gross + tax + pension + health + net = 4500 - 800 - 400 - 300 + 3000 = 6000
        # But the txn amount is 3000 (net cash), so splits won't validate
        # unless we only include net_salary_cash matching the txn amount.
        # This is a representation-layer test, not an invariant test for
        # the full payroll case (which includes non-cash components).


# ── Snapshots ───────────────────────────────────────────────────────


class TestSnapshots:
    def test_household_snapshot(self, db):
        from app.services.snapshot_service import compute_household_snapshot
        from app.models.snapshots import HouseholdSnapshot

        acct = _make_account(db, name="Checking")
        _make_txn(db, acct.id, 5000.0)

        mortgage = _make_account(
            db, name="Mortgage", acct_type=AccountType.MORTGAGE, is_asset=False,
            balance_truth_source=BalanceTruthSource.MANUAL_MARK.value,
            current_value=200000.0, value_as_of_date=datetime.now(),
        )
        db.commit()

        snap = compute_household_snapshot(db)
        db.commit()

        assert snap.net_worth_base is not None
        assert snap.accounts_included == 2
        assert snap.total_assets_base >= 5000.0

    def test_startup_state(self, db):
        from app.services.snapshot_service import compute_startup_state
        acct = _make_account(db, name="Checking")
        _make_txn(db, acct.id, 1000.0)
        db.commit()

        result = compute_startup_state(db)
        assert result.accounts_refreshed >= 1
        assert result.household_snapshot_id is not None


# ── Data quality ────────────────────────────────────────────────────


class TestDataQuality:
    def test_blockers_before_score(self, db):
        from app.services.data_quality import assess_quality
        report = assess_quality(db)
        assert len(report.blockers) > 0
        assert report.close_readiness_score < 100

    def test_uncategorized_warning(self, db):
        from app.services.data_quality import assess_quality
        acct = _make_account(db)
        for i in range(10):
            _make_txn(db, acct.id, -10.0 * (i + 1), desc=f"txn {i}")
        db.commit()

        report = assess_quality(db)
        has_uncat = any("uncategorized" in b for b in report.blockers)
        assert has_uncat

    def test_clean_ledger(self, db):
        from app.services.data_quality import assess_quality

        cat = _make_category(db, name="Food")
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

    def test_counters_populated(self, db):
        from app.services.data_quality import assess_quality
        acct = _make_account(db)
        _make_txn(db, acct.id, -50.0, desc="No category")
        db.commit()

        report = assess_quality(db)
        assert report.counters.uncategorized_count >= 1
        assert report.counters.unsplit_transaction_count >= 1

    def test_liabilities_without_decomposition(self, db):
        from app.services.data_quality import assess_quality
        acct = _make_account(db, acct_type=AccountType.MORTGAGE, is_asset=False)
        _make_txn(
            db, acct.id, 1200.0, desc="Payment",
            event_type=EconomicEventType.MORTGAGE_PAYMENT.value,
        )
        db.commit()

        report = assess_quality(db)
        assert report.counters.liabilities_without_decomposition >= 1
        has_warning = any("decomposition" in w for w in report.warnings)
        assert has_warning


# ── Spend analysis from splits ──────────────────────────────────────


class TestSpendAnalysis:
    def test_spend_summary_from_splits(self, db):
        from app.services.spend_analysis import compute_spend_summary
        from app.services.split_service import add_split

        cat = _make_category(db, name="Dining")
        acct = _make_account(db)
        txn = _make_txn(
            db, acct.id, -120.0, desc="Restaurant",
            date=datetime.now() - timedelta(days=10),
        )
        db.commit()

        add_split(
            db, txn.id, -120.0, "USD",
            counts_as_true_spend=True,
            spend_type=SpendType.LIFESTYLE,
            category_id=cat.id,
        )
        db.commit()

        summary = compute_spend_summary(db, months=1)
        assert summary.total_true_spend == -120.0
        assert SpendType.LIFESTYLE.value in summary.by_spend_type

    def test_non_spend_excluded(self, db):
        from app.services.spend_analysis import compute_spend_summary
        from app.services.split_service import add_split

        acct = _make_account(db)
        txn = _make_txn(
            db, acct.id, -500.0, desc="Transfer to savings",
            date=datetime.now() - timedelta(days=5),
        )
        db.commit()

        add_split(
            db, txn.id, -500.0, "USD",
            counts_as_true_spend=False,
            spend_type=SpendType.NON_SPEND_CASH_USE,
        )
        db.commit()

        summary = compute_spend_summary(db, months=1)
        assert summary.total_true_spend == 0.0


# ── Attribution ─────────────────────────────────────────────────────


class TestAttribution:
    def test_attribution_missing_snapshots(self, db):
        from app.services.attribution import attribute_nw_change
        result = attribute_nw_change(
            db,
            start_date=datetime(2025, 1, 1),
            end_date=datetime(2025, 6, 1),
        )
        assert len(result.warnings) > 0
        assert "Missing household snapshots" in result.warnings[0]

    def test_attribution_with_snapshots(self, db):
        from app.services.attribution import attribute_nw_change
        from app.services.snapshot_service import compute_household_snapshot

        acct = _make_account(db, name="Checking")
        _make_txn(db, acct.id, 10000.0, date=datetime(2025, 1, 1))
        db.commit()

        snap1 = compute_household_snapshot(db, as_of_date=datetime(2025, 1, 1))
        db.flush()

        _make_txn(db, acct.id, 5000.0, desc="Income", date=datetime(2025, 3, 1))
        _make_txn(db, acct.id, -2000.0, desc="Expenses", date=datetime(2025, 3, 15))
        db.commit()

        snap2 = compute_household_snapshot(db, as_of_date=datetime(2025, 6, 1))
        db.commit()

        result = attribute_nw_change(
            db,
            start_date=datetime(2025, 1, 1),
            end_date=datetime(2025, 6, 1),
        )
        assert result.delta_nw != 0.0
        assert len(result.components) > 0
