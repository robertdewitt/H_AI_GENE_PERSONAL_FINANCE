"""A charge dated on the statement day but posted after it comes after the anchor.

Card statements close by post date; the ledger keeps the transaction date. A
$509.14 hotel charge dated the day the June statement closed, posted the next
day, was excluded by "date > statement_date" for good — every later anchor
disagreed with the balance card by exactly that amount.
"""
import json
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.account_service import (
    _posted_after,
    get_account_balance_rich,
    get_many_account_balances_rich,
    get_many_account_balances_series,
)

STMT = datetime(2026, 6, 24)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _card(db):
    a = Account(name="Card", account_type=AccountType.CREDIT_CARD, currency="USD",
                is_asset=False, balance_truth_source="hybrid",
                statement_balance=Decimal("1782.64"), statement_balance_as_of=STMT)
    db.add(a)
    db.flush()
    return a


def _row(db, acct, date, amount, post_date=None, raw=True):
    raw_data = None
    if raw:
        raw_data = json.dumps({
            "Transaction Date": date.strftime("%m/%d/%Y"),
            "Post Date": post_date.strftime("%m/%d/%Y") if post_date else date.strftime("%m/%d/%Y"),
            "Description": "x", "Amount": str(amount),
        })
    t = Transaction(account_id=acct.id, date=date, description="x",
                    amount=Decimal(str(amount)), original_currency="USD", raw_data=raw_data)
    db.add(t)
    db.flush()
    return t


# ── the post-date reading ────────────────────────────────────────────────


def test_posted_next_day_counts_as_after():
    assert _posted_after({"Post Date": "06/25/2026"}, STMT)


def test_posted_same_day_is_on_the_statement():
    assert not _posted_after({"Post Date": "06/24/2026"}, STMT)


def test_day_first_sources_are_read_too():
    """25/06/2026 has no month-first reading; 06/25 has no day-first one."""
    assert _posted_after({"Post Date": "25/06/2026"}, STMT)


def test_no_post_date_means_no_change_in_treatment():
    assert not _posted_after({"Description": "x"}, STMT)
    assert not _posted_after(None, STMT)
    assert not _posted_after("not json", STMT)


def test_a_date_before_the_transaction_is_not_a_post_date():
    assert not _posted_after({"Post Date": "06/20/2026"}, STMT)


# ── through the three balance paths ─────────────────────────────────────


def test_anchor_day_charge_posted_later_is_in_the_balance(db):
    acct = _card(db)
    _row(db, acct, STMT, -509.14, post_date=datetime(2026, 6, 25))
    _row(db, acct, datetime(2026, 7, 1), -100.00)
    db.commit()

    single = get_account_balance_rich(db, acct.id).value
    batched = get_many_account_balances_rich(db, [acct])[acct.id].value

    assert single == Decimal("2391.78")            # 1782.64 + 509.14 + 100
    assert batched == single


def test_anchor_day_charge_posted_same_day_stays_on_the_statement(db):
    acct = _card(db)
    _row(db, acct, STMT, -509.14, post_date=STMT)
    db.commit()

    assert get_account_balance_rich(db, acct.id).value == Decimal("1782.64")
    assert get_many_account_balances_rich(db, [acct])[acct.id].value == Decimal("1782.64")


def test_anchor_day_row_without_source_data_keeps_the_old_treatment(db):
    acct = _card(db)
    _row(db, acct, STMT, -509.14, raw=False)
    db.commit()

    assert get_account_balance_rich(db, acct.id).value == Decimal("1782.64")


def test_the_series_agrees_with_the_balance_card(db):
    """The history used the wrong sign for liabilities as well — a month of
    spending read as paying the card off."""
    acct = _card(db)
    _row(db, acct, STMT, -509.14, post_date=datetime(2026, 6, 25))
    _row(db, acct, datetime(2026, 7, 1), -100.00)
    db.commit()

    series = get_many_account_balances_series(db, [acct], [datetime(2026, 7, 31)])
    assert series[datetime(2026, 7, 31)][acct.id].value == Decimal("2391.78")


def test_a_historical_as_of_before_the_statement_ignores_anchor_day_rows(db):
    acct = _card(db)
    _row(db, acct, STMT, -509.14, post_date=datetime(2026, 6, 25))
    db.commit()

    r = get_account_balance_rich(db, acct.id, as_of_date=datetime(2026, 6, 1))
    assert r.value == Decimal("1782.64")
