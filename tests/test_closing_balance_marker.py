"""The row that carries a day's closing balance is read off the chain, not the id.

Mission FCU's export lists a day newest-first, so the row imported last is the
day's earliest. Picking the highest id read a $7,721.07 balance on a day that
closed at $6,317.56, and every balance after it was $1,403.51 high.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.account_service import (
    _closing_row,
    get_account_balance_rich,
    get_many_account_balances_rich,
    get_many_account_balances_series,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _acct(db):
    a = Account(name="Checking", account_type=AccountType.CHECKING, currency="USD", is_asset=True)
    db.add(a); db.flush()
    return a


def _row(db, acct, day, amount, balance_after=None, desc="x"):
    t = Transaction(account_id=acct.id, date=datetime(2026, 9, day), description=desc,
                    amount=Decimal(str(amount)), original_currency="USD",
                    balance_after=Decimal(str(balance_after)) if balance_after is not None else None)
    db.add(t); db.flush()
    return t


def test_the_closing_row_is_the_one_nothing_follows():
    # opening 7061.57: +659.50 -> 7721.07, then -1403.51 -> 6317.56
    rows = [(2, Decimal("-1403.51"), Decimal("6317.56")), (3, Decimal("659.50"), Decimal("7721.07"))]
    assert _closing_row(rows)[0] == 2


def test_without_a_readable_chain_the_highest_id_wins():
    rows = [(1, Decimal("10"), Decimal("100")), (2, Decimal("10"), Decimal("500"))]
    assert _closing_row(rows)[0] == 2


def test_all_three_balance_paths_read_the_days_closing_balance(db):
    a = _acct(db)
    _row(db, a, 1, 3251.20, 7061.57)
    # Sep 2 arrives newest-first from the file: the closing row gets the lower id
    _row(db, a, 2, -1403.51, 6317.56)
    _row(db, a, 2, 659.50, 7721.07)
    _row(db, a, 3, -3290.00)              # confirmed schedule, no running balance yet
    db.commit()

    single = get_account_balance_rich(db, a.id).value
    batched = get_many_account_balances_rich(db, [a])[a.id].value
    series = get_many_account_balances_series(db, [a], [datetime(2026, 9, 30)])[datetime(2026, 9, 30)][a.id].value

    assert single == Decimal("3027.56")   # 6317.56 - 3290
    assert batched == single
    assert series == single


def test_the_series_reads_each_day_by_its_own_chain(db):
    a = _acct(db)
    _row(db, a, 2, -1403.51, 6317.56)
    _row(db, a, 2, 659.50, 7721.07)
    db.commit()
    at = datetime(2026, 9, 2)
    assert get_many_account_balances_series(db, [a], [at])[at][a.id].value == Decimal("6317.56")
