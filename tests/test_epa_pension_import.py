"""WTW ePA pension statement parsing, import, and valuation."""
import pytest
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.asset_valuation import AssetValuation
from app.models.instrument import Instrument, PositionLot
from app.services.epa_pension_import import (
    import_pension_positions,
    parse_epa_pension_text,
    value_pension_account,
)

# Mirrors pdfplumber's text extraction of the ePA "My Fund Balance" PDF.
TEXT = """My Savings
Balance By Fund
Total Value: £364,812.79
u North American Equity-Passive £189,719.53
u Global Equity - Active £112,341.69
u World (ex-UK) Equity - Passive £62,751.57
Fund Units Unit Unit Price Fund Value in Fund
North 15,374.35 12.34 22/07/2026 189,719.53 £189,719.53 52.01 Details
Global 15,393.49 7.298 22/07/2026 112,341.69 £112,341.69 30.79 Details
World 6,477.24 9.688 22/07/2026 62,751.57 £62,751.57 17.20 Details
"""


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _pension_account(db):
    a = Account(name="ePA Pension", account_type=AccountType.PENSION, currency="GBP", is_asset=True)
    db.add(a)
    db.flush()
    return a


def test_parse_funds_and_total():
    p = parse_epa_pension_text(TEXT)
    assert p.total_value == Decimal("364812.79")
    assert p.currency == "GBP"
    assert len(p.funds) == 3
    names = {f.name for f in p.funds}
    assert "North American Equity-Passive" in names
    assert "Global Equity - Active" in names
    f0 = p.funds[0]
    assert f0.units == Decimal("15374.35")
    assert f0.unit_price == Decimal("12.34")
    assert f0.value == Decimal("189719.53")


def test_import_values_to_statement_total(db):
    acct = _pension_account(db)
    p = parse_epa_pension_text(TEXT)
    stats = import_pension_positions(db, acct, p)

    assert stats["funds"] == 3
    # Precise implied prices make the sum reconcile to the statement total.
    assert stats["valuation"]["value"] == Decimal("364812.79")
    assert acct.balance_truth_source == "latest_valuation"

    # One instrument + one position lot per fund.
    assert len(db.execute(select(Instrument)).scalars().all()) == 3
    assert len(db.execute(select(PositionLot)).scalars().all()) == 3


def test_reimport_replaces_not_stacks(db):
    acct = _pension_account(db)
    import_pension_positions(db, acct, parse_epa_pension_text(TEXT))
    import_pension_positions(db, acct, parse_epa_pension_text(TEXT))
    # Still 3 lots (old ePA lots cleared first), not 6.
    assert len(db.execute(select(PositionLot)).scalars().all()) == 3


def test_value_read_only_does_not_persist(db):
    acct = _pension_account(db)
    import_pension_positions(db, acct, parse_epa_pension_text(TEXT))
    before = len(db.execute(select(AssetValuation)).scalars().all())
    value_pension_account(db, acct, persist=False)
    after = len(db.execute(select(AssetValuation)).scalars().all())
    assert before == after
