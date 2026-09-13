"""Revolut Bank UK Ltd statements (post-migration) import like the old ones.

After the August 2026 migration the statement writes "5 Sept 2026" where the
e-money one wrote "5 Sep 2026", and opens with a page-long notice so the
statement proper starts on page two. The September file parsed to zero rows
and was not even recognised as Revolut. Separately, Revolut names its
per-currency files by hash, and the USD and GBP statements were dropped on
each other's accounts.
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.services import revolut_import
from app.services.revolut_pdf_parser import _parse_date, statement_currency_from_text


def test_a_four_letter_month_is_read():
    d = _parse_date(["5", "Sept", "2026"])
    assert d is not None and (d.year, d.month, d.day) == (2026, 9, 5)


def test_three_letter_months_still_work():
    assert _parse_date(["14", "Jun", "2024"]).month == 6


def test_an_unknown_month_is_still_rejected():
    assert _parse_date(["27", "Mrz", "2026"]) is None


def test_the_statement_currency_is_read_from_the_header():
    assert statement_currency_from_text("GBP Statement\nGenerated on the 13 Sept 2026") == "GBP"
    assert statement_currency_from_text("USD Statement\nGenerated on") == "USD"
    assert statement_currency_from_text("PERSONAL ACCOUNT MIGRATION\n...") is None


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_a_statement_is_imported_through_the_standard_path(db, tmp_path, monkeypatch):
    """The confirm step goes through import_transactions: duplicates, running
    balances and the batch's source are all handled there."""
    acct = Account(name="Revolut GBP", account_type=AccountType.SAVINGS, currency="GBP", is_asset=True)
    db.add(acct); db.flush()
    import datetime as dt
    rows = [
        {"date": dt.datetime(2026, 9, 5), "description": "Restaurant Associates", "amount": Decimal("-2.50"), "balance": Decimal("103.21"), "section": "main"},
        {"date": dt.datetime(2026, 9, 8), "description": "Transfer from SEIREI DE WITT", "amount": Decimal("1200.00"), "balance": Decimal("1221.34"), "section": "main"},
    ]
    monkeypatch.setattr(revolut_import, "parse_revolut_pdf", lambda path, include_sections=None: rows)
    monkeypatch.setattr(revolut_import, "revolut_statement_currency", lambda path: "GBP")
    pdf = tmp_path / "statement.pdf"; pdf.write_bytes(b"%PDF-1.4 stub")

    batch = revolut_import.import_revolut_statement(db, acct.id, str(pdf), {"main"}, "GBP")

    got = db.execute(select(Transaction).where(Transaction.account_id == acct.id).order_by(Transaction.date)).scalars().all()
    assert [(t.description, t.amount, t.balance_after, t.original_currency) for t in got] == [
        ("Restaurant Associates", Decimal("-2.50"), Decimal("103.21"), "GBP"),
        ("Transfer from SEIREI DE WITT", Decimal("1200.00"), Decimal("1221.34"), "GBP"),
    ]
    assert batch.source == "revolut_pdf" and batch.filename == "statement.pdf" and batch.row_count == 2
    assert not (tmp_path / "statement.rows.csv").exists()      # the scratch CSV is not left behind

    again = revolut_import.import_revolut_statement(db, acct.id, str(pdf), {"main"}, "GBP")
    assert again.row_count == 0                                  # a second run is all duplicates
