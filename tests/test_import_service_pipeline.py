"""End-to-end tests for the CSV/XLS/XLSX import pipeline.

Failure modes that matter in a truth engine are *silently wrong numbers*,
not exceptions — these tests assert totals and counts, not just
"no exception".

Covers:
- Date-format auto-detection: DD/MM vs MM/DD, and ambiguous input
- Liability sign flip: positive charges in the file → negative in DB
- Batch insert correctness: counts, totals, dedupe on re-import
- Column detection: amount-vs-debit/credit shape
"""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.import_service import (
    detect_columns,
    detect_date_format,
    import_transactions,
    parse_amount,
    parse_date,
    read_file,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _checking(db, currency="USD"):
    a = Account(
        name="Checking", account_type=AccountType.CHECKING,
        currency=currency, is_asset=True,
    )
    db.add(a)
    db.flush()
    return a


def _credit_card(db, currency="GBP"):
    a = Account(
        name="Card", account_type=AccountType.CREDIT_CARD,
        currency=currency, is_asset=False,
    )
    db.add(a)
    db.flush()
    return a


def _write_csv(tmp_path: Path, rows: list[str]) -> Path:
    p = tmp_path / "in.csv"
    p.write_text("\n".join(rows))
    return p


# ── Date format detection ─────────────────────────────────────────────


def test_dayfirst_detected_from_european_dates():
    detection = detect_date_format([
        "31/01/2026", "28/02/2026", "15/03/2026", "30/04/2026",
    ])
    assert detection.dayfirst is True
    assert detection.confidence in ("high", "medium")


def test_monthfirst_detected_from_us_dates():
    detection = detect_date_format([
        "01/31/2026", "02/28/2026", "03/15/2026", "04/30/2026",
    ])
    assert detection.dayfirst is False


def test_ambiguous_dates_pick_a_side_with_reason():
    # All entries are valid under both interpretations — detector must
    # still emit a single deterministic choice and explain itself.
    detection = detect_date_format([
        "01/02/2026", "03/04/2026", "05/06/2026",
    ])
    assert detection.dayfirst in (True, False)
    assert detection.reasoning  # some explanation should be present


def test_parse_date_respects_dayfirst():
    assert parse_date("01/02/2026", dayfirst=True)  == datetime(2026, 2, 1)
    assert parse_date("01/02/2026", dayfirst=False) == datetime(2026, 1, 2)


def test_parse_amount_handles_decimal_strings():
    assert parse_amount("1,234.56") == Decimal("1234.56")
    assert parse_amount("-99.00")   == Decimal("-99.00")
    assert parse_amount("$1,000")   == Decimal("1000")
    assert parse_amount("")        is None


# ── Column detection ──────────────────────────────────────────────────


def test_column_detection_amount_shape(tmp_path):
    p = _write_csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,-4.50",
        "02/01/2026,Salary,2000.00",
    ])
    df = read_file(str(p))
    mapping = detect_columns(df)
    assert mapping["date"] == "Date"
    assert mapping["description"] == "Description"
    assert mapping["amount"] == "Amount"


def test_column_detection_debit_credit_shape(tmp_path):
    p = _write_csv(tmp_path, [
        "Date,Description,Debit,Credit",
        "01/01/2026,Coffee,4.50,",
        "02/01/2026,Salary,,2000.00",
    ])
    df = read_file(str(p))
    mapping = detect_columns(df)
    assert mapping["date"] == "Date"
    assert mapping["description"] == "Description"
    assert mapping["debit"] == "Debit"
    assert mapping["credit"] == "Credit"


# ── End-to-end import: totals, counts, sign convention, dedupe ────────


def test_import_asset_account_preserves_signs(db, tmp_path):
    acct = _checking(db)
    p = _write_csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,-4.50",
        "02/01/2026,Salary,2000.00",
        "03/01/2026,Refund,12.34",
    ])
    batch = import_transactions(
        db, acct.id, str(p),
        column_mapping={"date": "Date", "description": "Description", "amount": "Amount"},
        account_currency="USD",
        is_liability=False,
        dayfirst=True,
    )
    txns = db.execute(
        select(Transaction).where(Transaction.account_id == acct.id)
        .order_by(Transaction.date)
    ).scalars().all()

    assert batch.row_count == 3
    assert [t.amount for t in txns] == [
        Decimal("-4.50"), Decimal("2000.00"), Decimal("12.34"),
    ]
    assert sum((t.amount for t in txns), Decimal("0")) == Decimal("2007.84")


def test_import_liability_account_flips_positive_to_negative(db, tmp_path):
    """For a credit card, positive amounts in the file are charges (debt
    increases), stored as negative so the account balance is the right sign."""
    card = _credit_card(db)
    p = _write_csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,4.50",
        "05/01/2026,Restaurant,80.00",
        "10/01/2026,Payment,-150.00",
    ])
    import_transactions(
        db, card.id, str(p),
        column_mapping={"date": "Date", "description": "Description", "amount": "Amount"},
        account_currency="GBP",
        is_liability=True,
        dayfirst=True,
    )
    txns = db.execute(
        select(Transaction).where(Transaction.account_id == card.id)
        .order_by(Transaction.date)
    ).scalars().all()

    # Original signs flipped: charges become negative, payment becomes positive.
    assert [t.amount for t in txns] == [
        Decimal("-4.50"), Decimal("-80.00"), Decimal("150.00"),
    ]


def test_reimporting_same_file_skips_duplicates(db, tmp_path):
    acct = _checking(db)
    p = _write_csv(tmp_path, [
        "Date,Description,Amount",
        "01/01/2026,Coffee,-4.50",
        "02/01/2026,Salary,2000.00",
    ])
    mapping = {"date": "Date", "description": "Description", "amount": "Amount"}
    import_transactions(db, acct.id, str(p), mapping, "USD", False, True)
    import_transactions(db, acct.id, str(p), mapping, "USD", False, True)

    txns = db.execute(
        select(Transaction).where(Transaction.account_id == acct.id)
    ).scalars().all()
    assert len(txns) == 2, "second import of identical rows must dedupe"


def test_import_debit_credit_columns_yield_signed_amounts(db, tmp_path):
    acct = _checking(db)
    p = _write_csv(tmp_path, [
        "Date,Description,Debit,Credit",
        "01/01/2026,Coffee,4.50,",
        "02/01/2026,Salary,,2000.00",
    ])
    import_transactions(
        db, acct.id, str(p),
        column_mapping={
            "date": "Date", "description": "Description",
            "debit": "Debit", "credit": "Credit",
        },
        account_currency="USD",
        is_liability=False,
        dayfirst=True,
    )
    txns = db.execute(
        select(Transaction).order_by(Transaction.date)
    ).scalars().all()
    # Debit is money out (negative); credit is money in (positive).
    assert [t.amount for t in txns] == [Decimal("-4.50"), Decimal("2000.00")]
