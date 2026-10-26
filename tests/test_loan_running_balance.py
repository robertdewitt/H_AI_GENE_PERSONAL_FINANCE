"""Loan ledger running balance, anchored to statements."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.payment_decomposition import PaymentDecomposition
from app.models.snapshots import LiabilityBalanceSnapshot
from app.models.transaction import Transaction
from app.services.loan_balance import loan_running_balances


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _loan(db):
    loan = Account(name="Mortgage", account_type=AccountType.MORTGAGE, currency="USD", is_asset=False)
    db.add(loan)
    db.flush()
    return loan


def _snap(db, loan, d, owed):
    db.add(LiabilityBalanceSnapshot(account_id=loan.id, as_of_date=datetime.combine(d, datetime.min.time()),
                                    value_native=Decimal(owed), value_base=Decimal(owed), currency="USD",
                                    fx_rate=1.0, source="mortgage_statement_pdf", confidence=1.0, stale_flag=False))


def _pay(db, loan, d, amount, principal=None):
    t = Transaction(account_id=loan.id, date=datetime.combine(d, datetime.min.time()), description="Payment",
                    amount=Decimal(amount), original_currency="USD")
    db.add(t)
    db.flush()
    if principal is not None:
        db.add(PaymentDecomposition(transaction_id=t.id, component="principal", amount=Decimal(principal),
                                    currency="USD"))
    return t


def test_payments_reduce_by_principal_and_land_on_statements(db):
    loan = _loan(db)
    _snap(db, loan, date(2026, 2, 12), "205952.96")
    mar = _pay(db, loan, date(2026, 3, 3), "3290.00", principal="2860.93")
    _snap(db, loan, date(2026, 3, 12), "203092.03")
    apr = _pay(db, loan, date(2026, 4, 3), "3290.00", principal="2866.89")
    db.flush()

    bal = loan_running_balances(db, loan)
    assert bal[mar.id] == Decimal("203092.03")               # backed off the March statement
    assert bal[apr.id] == Decimal("203092.03") - Decimal("2866.89")   # forward from the latest


def test_rows_before_the_first_statement_work_back_from_it(db):
    loan = _loan(db)
    jan = _pay(db, loan, date(2026, 1, 3), "3290.00", principal="2850.00")
    feb = _pay(db, loan, date(2026, 2, 3), "3290.00", principal="2855.00")
    _snap(db, loan, date(2026, 2, 12), "205952.96")
    db.flush()

    bal = loan_running_balances(db, loan)
    assert bal[feb.id] == Decimal("205952.96")
    assert bal[jan.id] == Decimal("205952.96") + Decimal("2855.00")


def test_without_a_breakdown_the_full_amount_counts(db):
    loan = _loan(db)
    _snap(db, loan, date(2026, 2, 12), "1000.00")
    t = _pay(db, loan, date(2026, 3, 3), "100.00")
    db.flush()
    assert loan_running_balances(db, loan)[t.id] == Decimal("900.00")


def test_non_loan_accounts_get_nothing(db):
    acct = Account(name="Checking", account_type=AccountType.CHECKING, currency="USD", is_asset=True)
    db.add(acct)
    db.flush()
    assert loan_running_balances(db, acct) == {}


def test_statement_export_with_negative_payments_and_carried_forward_row(db):
    """Lourdes-style ledger: payments are debits, a row carries the balance."""
    loan = _loan(db)
    jul = _pay(db, loan, date(2025, 7, 31), "-2656.24")
    aug = _pay(db, loan, date(2025, 8, 29), "-2656.24")
    cf = Transaction(account_id=loan.id, date=datetime(2025, 9, 30), description="Balance carried forward",
                     amount=Decimal("-846393.78"), original_currency="USD")
    db.add(cf)
    db.flush()

    bal = loan_running_balances(db, loan)
    assert bal[cf.id] == Decimal("846393.78")
    assert bal[aug.id] == Decimal("846393.78")
    assert bal[jul.id] == Decimal("846393.78") + Decimal("2656.24")
