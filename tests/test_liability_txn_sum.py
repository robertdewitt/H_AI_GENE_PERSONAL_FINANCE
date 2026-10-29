"""A liability whose balance is the sum of its rows reports the amount owed."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.account_service import get_account_balance, get_many_account_balances_series


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _loan(db):
    loan = Account(name="Car loan", account_type=AccountType.LOAN, currency="USD", is_asset=False,
                   balance_truth_source="transaction_sum", current_value=Decimal("54846.61"))
    db.add(loan)
    db.flush()
    for d, amt in ((datetime(2026, 6, 30), "-54846.00"), (datetime(2026, 7, 29), "636.63"),
                   (datetime(2026, 9, 1), "636.63")):
        db.add(Transaction(account_id=loan.id, date=d, description="row", amount=Decimal(amt),
                           original_currency="USD"))
    db.flush()
    return loan


def test_loan_balance_is_amount_owed(db):
    loan = _loan(db)
    assert get_account_balance(db, loan.id) == Decimal("53572.74")


def test_history_owes_nothing_before_the_loan_and_owed_after(db):
    loan = _loan(db)
    dates = [datetime(2026, 6, 1), datetime(2026, 8, 1)]
    series = get_many_account_balances_series(db, [loan], dates)
    assert series[dates[0]][loan.id].value == Decimal("0.00")
    assert series[dates[1]][loan.id].value == Decimal("54209.37")


def test_asset_sum_is_unchanged(db):
    acct = Account(name="Checking", account_type=AccountType.CHECKING, currency="USD", is_asset=True,
                   balance_truth_source="transaction_sum")
    db.add(acct)
    db.flush()
    db.add(Transaction(account_id=acct.id, date=datetime(2026, 6, 1), description="pay", amount=Decimal("-10"),
                       original_currency="USD"))
    db.flush()
    assert get_account_balance(db, acct.id) == Decimal("-10")
