"""Re-importing the same statement must not land a second copy.

The importer's original duplicate check keyed on (date, description, amount)
read off the *parsed* row, so it moved whenever parsing did — a re-export with
different spacing, a liability sign convention read differently between two
files, a date column parsed DD/MM one time and MM/DD the next. Fingerprinting
the source row is immune to all of that, because the bytes in the file did not
change.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.import_service import import_transactions
from app.services.source_fingerprint import backfill, canonical_row, fingerprint


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _account(db, is_asset=True, kind=AccountType.CHECKING):
    a = Account(name="Acct", account_type=kind, currency="USD", is_asset=is_asset)
    db.add(a)
    db.flush()
    return a


def _csv(tmp_path, lines, name="stmt.csv"):
    p = tmp_path / name
    p.write_text("\n".join(lines))
    return str(p)


MAPPING = {"date": "Date", "description": "Description", "amount": "Amount"}


def _import(db, acct, path, **kw):
    return import_transactions(
        db, acct.id, path, column_mapping=MAPPING, account_currency="USD",
        dayfirst=True, **kw,
    )


def _count(db, acct):
    return len(db.execute(
        select(Transaction).where(Transaction.account_id == acct.id)
    ).scalars().all())


# ── The fingerprint itself ───────────────────────────────────────────────


def test_column_order_does_not_change_the_fingerprint():
    a = {"Date": "01/01/2026", "Amount": "-4.50", "Description": "Coffee"}
    b = {"Description": "Coffee", "Date": "01/01/2026", "Amount": "-4.50"}

    assert fingerprint(1, a) == fingerprint(1, b)


def test_the_same_row_on_a_different_account_is_a_different_fingerprint():
    row = {"Date": "01/01/2026", "Description": "Coffee", "Amount": "-4.50"}

    assert fingerprint(1, row) != fingerprint(2, row)


def test_repeats_of_one_row_get_distinct_fingerprints():
    """A statement may legitimately list the same charge twice."""
    row = {"Date": "01/01/2026", "Description": "Parking", "Amount": "-3.75"}

    assert fingerprint(1, row, 0) != fingerprint(1, row, 1)


def test_canonical_row_trims_but_keeps_content():
    assert canonical_row({" Date ": " 01/01/2026 "}) == canonical_row(
        {"Date": "01/01/2026"}
    )


# ── Re-importing ─────────────────────────────────────────────────────────


def test_reimporting_the_identical_file_adds_nothing(db, tmp_path):
    acct = _account(db)
    path = _csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,-4.50",
        "02/01/2026,Salary,2000.00",
    ])
    _import(db, acct, path)
    assert _count(db, acct) == 2

    batch = _import(db, acct, path)

    assert _count(db, acct) == 2
    assert batch.row_count == 0


def test_a_statement_listing_the_same_charge_twice_keeps_both(db, tmp_path):
    """The old key collapsed these into one — two identical parking charges on
    the same day are two real transactions."""
    acct = _account(db)
    path = _csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,SAN DIEGO PARKING,-3.75",
        "01/01/2026,SAN DIEGO PARKING,-3.75",
    ])

    _import(db, acct, path)

    assert _count(db, acct) == 2


def test_reimporting_that_file_still_adds_nothing(db, tmp_path):
    acct = _account(db)
    path = _csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,SAN DIEGO PARKING,-3.75",
        "01/01/2026,SAN DIEGO PARKING,-3.75",
    ])
    _import(db, acct, path)
    assert _count(db, acct) == 2

    _import(db, acct, path)

    assert _count(db, acct) == 2


def test_every_imported_row_is_stamped(db, tmp_path):
    acct = _account(db)
    _import(db, acct, _csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,-4.50",
    ]))

    rows = db.execute(select(Transaction)).scalars().all()
    assert all(t.source_hash for t in rows)


# ── Backfill ─────────────────────────────────────────────────────────────


def test_backfill_stamps_rows_imported_before_the_column_existed(db, tmp_path):
    acct = _account(db)
    path = _csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,-4.50",
        "02/01/2026,Salary,2000.00",
    ])
    _import(db, acct, path)
    # Simulate the pre-existing ledger: raw_data present, no fingerprint.
    for txn in db.execute(select(Transaction)).scalars().all():
        txn.source_hash = None
    db.commit()

    stamped = backfill(db)
    db.commit()

    assert stamped == 2
    assert all(t.source_hash for t in db.execute(select(Transaction)).scalars().all())


def test_backfilled_rows_block_a_reimport(db, tmp_path):
    """This is the point of the backfill — old files gain the protection too."""
    acct = _account(db)
    path = _csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,-4.50",
    ])
    _import(db, acct, path)
    for txn in db.execute(select(Transaction)).scalars().all():
        txn.source_hash = None
    db.commit()
    backfill(db)
    db.commit()

    _import(db, acct, path)

    assert _count(db, acct) == 1


def test_backfill_is_idempotent(db, tmp_path):
    acct = _account(db)
    _import(db, acct, _csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,-4.50",
    ]))
    db.commit()

    assert backfill(db) == 0
