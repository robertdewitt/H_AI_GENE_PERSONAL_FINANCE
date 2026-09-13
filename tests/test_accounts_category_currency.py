"""Category totals on the accounts page are shown in the display currency.

The income and spend charts summed native amounts across accounts in
different currencies and put the display currency's symbol in front — a
£16,280 salary and a $3,765 rent were added as if they were the same money.
"""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.currency_rate import CurrencyRate
from app.routers.accounts import _in_display_currency


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(CurrencyRate(base_currency="GBP", quote_currency="USD",
                             date=datetime(2026, 8, 15), rate=1.25, source="test"))
    session.commit()
    yield session
    session.close()


def _row(month, category, total, currency):
    return SimpleNamespace(month=month, category=category, total=Decimal(str(total)), currency=currency)


def test_each_bucket_is_converted_and_merged_by_category(db):
    rows = [_row("2026-08", "Salary", 16280, "GBP"), _row("2026-08", "Salary", 3765, "USD")]

    out = _in_display_currency(db, rows, "USD")

    assert [(r.month, r.category, r.total) for r in out] == [("2026-08", "Salary", Decimal("24115.00"))]


def test_a_missing_rate_leaves_the_amount_unconverted_and_says_so(db):
    rows = [_row("2026-08", "Rent", 100, "JPY")]

    out = _in_display_currency(db, rows, "USD")

    assert out[0].total == Decimal("100") and out[0].unconverted is True


def test_same_currency_passes_through(db):
    rows = [_row("2026-08", "Rent", 3765, "USD")]
    assert _in_display_currency(db, rows, "USD")[0].total == Decimal("3765")
