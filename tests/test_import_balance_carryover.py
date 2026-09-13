"""Rows the user put on the ledger ahead of the bank meet the bank's row.

A confirmed schedule occurrence or a manual entry has no running balance and
no source file. When the export that contains the real row arrives, that row
used to be either skipped as a duplicate (so the running balance never reached
the ledger and the account kept reading from an older day) or, if the user had
worded it differently, imported as a second copy. Mission FCU had both: a
"Mission FCU" payment skipped without its $3,027.56 closing balance, and an
"Emergency Repair" the user entered on Aug 31 sitting beside the bank's
"8 West Property" of Sep 1 for the same $1,403.51.
"""
import json
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.import_service import import_transactions

MAPPING = {"date": "Post Date", "description": "Description", "debit": "Debit",
           "credit": "Credit", "balance": "Balance"}


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


def _placeholder(db, acct, date, amount, desc, scheduled=False):
    raw = json.dumps({"source": "scheduled_confirm", "scheduled_payment_id": 1}) if scheduled else None
    t = Transaction(account_id=acct.id, date=date, description=desc, amount=Decimal(str(amount)),
                    original_currency="USD", raw_data=raw)
    db.add(t); db.flush()
    return t


def _csv(tmp_path, lines):
    p = tmp_path / "AccountHistory.csv"
    p.write_text("Post Date,Description,Debit,Credit,Balance\n" + "\n".join(lines) + "\n")
    return str(p)


def _import(db, acct, path):
    return import_transactions(db, acct.id, path, column_mapping=MAPPING,
                               account_currency="USD", is_liability=False, dayfirst=False)


def _rows(db, acct):
    return db.execute(select(Transaction).where(Transaction.account_id == acct.id)
                      .order_by(Transaction.date, Transaction.id)).scalars().all()


def test_an_exact_repeat_of_a_confirmed_occurrence_hands_over_its_balance(db, tmp_path):
    acct = _acct(db)
    ph = _placeholder(db, acct, datetime(2026, 9, 3), -3290, "Mission FCU", scheduled=True)
    db.commit()

    batch = _import(db, acct, _csv(tmp_path, ["9/3/2026,Mission FCU,3290.00,,3027.56"]))

    rows = _rows(db, acct)
    assert len(rows) == 1 and rows[0].id == ph.id
    assert rows[0].balance_after == Decimal("3027.56")
    assert rows[0].import_batch_id == batch.id
    assert batch._balances_carried == 1


def test_a_manual_entry_meets_the_banks_row_a_day_later(db, tmp_path):
    acct = _acct(db)
    ph = _placeholder(db, acct, datetime(2026, 8, 31), -1403.51, "Emergency Repair")
    db.commit()

    batch = _import(db, acct, _csv(tmp_path, ["9/1/2026,8 West Property,1403.51,,6317.56"]))

    rows = _rows(db, acct)
    assert len(rows) == 1 and rows[0].id == ph.id
    assert rows[0].description == "Emergency Repair"          # the user's wording stays
    assert rows[0].date == datetime(2026, 9, 1)               # the bank's date wins
    assert rows[0].balance_after == Decimal("6317.56")
    assert "8 West Property" in rows[0].raw_data
    assert batch._placeholders_reconciled == 1 and batch.row_count == 0


def test_a_manual_entry_too_far_away_is_not_the_same_transaction(db, tmp_path):
    acct = _acct(db)
    _placeholder(db, acct, datetime(2026, 8, 20), -1403.51, "Emergency Repair")
    db.commit()

    _import(db, acct, _csv(tmp_path, ["9/1/2026,8 West Property,1403.51,,6317.56"]))

    assert len(_rows(db, acct)) == 2


def test_a_schedule_occurrence_is_allowed_a_wider_window(db, tmp_path):
    acct = _acct(db)
    ph = _placeholder(db, acct, datetime(2026, 9, 3), -3290, "Mission FCU", scheduled=True)
    db.commit()

    _import(db, acct, _csv(tmp_path, ["9/9/2026,Mission FCU Loan,3290.00,,100.00"]))

    rows = _rows(db, acct)
    assert len(rows) == 1 and rows[0].id == ph.id and rows[0].balance_after == Decimal("100.00")


def test_each_expectation_is_used_once(db, tmp_path):
    acct = _acct(db)
    _placeholder(db, acct, datetime(2026, 9, 1), -100, "Expected")
    db.commit()

    _import(db, acct, _csv(tmp_path, ["9/1/2026,Shop A,100.00,,900.00", "9/2/2026,Shop B,100.00,,800.00"]))

    rows = _rows(db, acct)
    assert len(rows) == 2
    assert [r.description for r in rows] == ["Expected", "Shop B"]


def test_a_row_that_already_has_its_balance_is_left_alone(db, tmp_path):
    acct = _acct(db)
    t = _placeholder(db, acct, datetime(2026, 9, 3), -3290, "Mission FCU")
    t.balance_after = Decimal("1.00"); t.import_batch_id = None
    db.commit()

    _import(db, acct, _csv(tmp_path, ["9/3/2026,Mission FCU,3290.00,,3027.56"]))

    rows = _rows(db, acct)
    assert len(rows) == 1 and rows[0].balance_after == Decimal("1.00")
