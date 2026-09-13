"""A re-import never brings back a row the user deleted.

The duplicate layers only knew rows still on the ledger. A Mission FCU
export re-run to pick up running balances re-inserted twenty-six rows the
user had removed as duplicates three times over — their surviving twins were
in an older export's wording ("WithdrawalACHMISSION FCU" for "Mission FCU"),
which the near-duplicate layer did not recognise either.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.deleted_transaction import DeletedTransaction
from app.models.transaction import Transaction
from app.services.import_service import _wording_matches_existing, import_transactions

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


def _csv(tmp_path, lines):
    p = tmp_path / "AccountHistory.csv"
    p.write_text("Post Date,Description,Debit,Credit,Balance\n" + "\n".join(lines) + "\n")
    return str(p)


def _import(db, acct, path):
    return import_transactions(db, acct.id, path, column_mapping=MAPPING,
                               account_currency="USD", is_liability=False, dayfirst=False)


def _rows(db, acct):
    return db.execute(select(Transaction).where(Transaction.account_id == acct.id)).scalars().all()


def test_a_row_the_user_deleted_is_not_imported_again(db, tmp_path):
    acct = _acct(db)
    db.add(DeletedTransaction(original_id=999, account_id=acct.id, date=datetime(2026, 7, 3),
                              description="Mission FCU", amount=Decimal("-3290"), original_currency="USD"))
    db.commit()

    batch = _import(db, acct, _csv(tmp_path, ["7/3/2026,Mission FCU,3290.00,,1366.68"]))

    assert _rows(db, acct) == []
    assert batch.row_count == 0 and batch._previously_deleted == 1


def test_a_deleted_row_worded_differently_is_still_remembered(db, tmp_path):
    acct = _acct(db)
    db.add(DeletedTransaction(original_id=999, account_id=acct.id, date=datetime(2026, 7, 3),
                              description="Mission FCU Loan Pmt", amount=Decimal("-3290"), original_currency="USD"))
    db.commit()

    _import(db, acct, _csv(tmp_path, ["7/3/2026,Mission FCU,3290.00,,1366.68"]))

    assert _rows(db, acct) == []


def test_the_older_export_wording_is_the_same_row():
    """One export writes "Mission FCU", an older one "WithdrawalACHMISSION FCU"."""
    assert _wording_matches_existing("Mission FCU", ["withdrawalachmission fcu"])
    assert _wording_matches_existing("Alta Vista Prope", ["depositachaltavistaprope"])


def test_a_short_word_contained_in_another_is_not_enough():
    assert not _wording_matches_existing("Uber", ["withdrawalachuber eats london"])


def test_the_surviving_twin_receives_the_running_balance(db, tmp_path):
    acct = _acct(db)
    t = Transaction(account_id=acct.id, date=datetime(2026, 7, 3), description="WithdrawalACHMISSION FCU",
                    amount=Decimal("-3290"), original_currency="USD")
    db.add(t); db.commit()

    batch = _import(db, acct, _csv(tmp_path, ["7/3/2026,Mission FCU,3290.00,,1366.68"]))

    rows = _rows(db, acct)
    assert len(rows) == 1 and rows[0].id == t.id
    assert rows[0].balance_after == Decimal("1366.68")
    assert batch._balances_carried == 1
