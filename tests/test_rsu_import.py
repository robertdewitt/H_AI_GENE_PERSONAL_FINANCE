"""Merrill RSU award-summary parsing, import, and valuation."""
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.asset_valuation import AssetValuation
from app.models.instrument import PriceSnapshot
from app.models.rsu import RSUGrant, RSUVest
from app.services.rsu_service import (
    import_rsu_grants,
    parse_merrill_rsu_text,
    value_rsu_account,
)

CSV = """﻿"Your Awards"
"Exported on: "07/23/2026 06:55 AM
,
"Estimated remaining value","$464,138.37"
"Stock symbol","BAC",
"Stock Price as of date",07/22/2026,
"Stock Price","$61.62",
,
"Restricted Stock Units","Awarded Units","Units unvested","Unvested estimated value","Units vested","Unpaid cash dividends & interest"
,"3,000","2,000","$123,240.00","1,000","$0.00"
,
"Award date","Award type/code","Next Vest Date","Awarded Units","Units unvested","Unvested estimated value","Units vested"
"02/13/2026","Restricted Stock Units / 26PG1BI","02/15/2027","2,000","2,000","$123,240.00","0"
,,,,,,,"Vesting date","Units vesting","Units unvested","Unvested estimated value","Units vested"
,,,,,,,"02/15/2027","1,000","1,000","$61,620.00","0"
,,,,,,,"02/15/2028","1,000","1,000","$61,620.00","0"
"02/15/2024","Restricted Stock Units / 24PG1BI","02/15/2027","1,000","0","$0.00","1,000"
,,,,,,,"02/15/2025","1,000","0","$0.00","1,000"
"Total",,,"3,000","2,000","$123,240.00","1,000"
"""


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _rsu_account(db):
    a = Account(name="BofA RSUs", account_type=AccountType.RSU, currency="USD", is_asset=True)
    db.add(a)
    db.flush()
    return a


def test_parse_totals_and_grants():
    p = parse_merrill_rsu_text(CSV)
    assert p.symbol == "BAC"
    assert p.stock_price == Decimal("61.62")
    assert p.price_date == date(2026, 7, 22)
    assert p.total_awarded == Decimal("3000")
    assert p.total_unvested == Decimal("2000")
    assert p.total_vested == Decimal("1000")
    assert len(p.grants) == 2
    assert p.grants[0].award_code == "26PG1BI"
    assert len(p.grants[0].vests) == 2
    assert p.grants[0].vests[0].units == Decimal("1000")


def test_import_creates_grants_and_values_unvested(db):
    acct = _rsu_account(db)
    p = parse_merrill_rsu_text(CSV)
    stats = import_rsu_grants(db, acct, p)

    assert stats["grants_created"] == 2
    assert stats["vests_created"] == 3
    # Valuation = unvested units (2000) × statement price (61.62).
    assert stats["valuation"]["units_unvested"] == Decimal("2000")
    assert stats["valuation"]["value"] == Decimal("123240.00")

    # Balance now sources from AssetValuation.
    assert acct.balance_truth_source == "latest_valuation"
    val = db.execute(select(AssetValuation).where(
        AssetValuation.account_id == acct.id)).scalars().all()
    assert len(val) == 1
    assert val[0].value == Decimal("123240.00")
    # Statement price snapshot recorded for the instrument.
    assert db.execute(select(PriceSnapshot)).scalars().first().price == Decimal("61.62")


def test_reimport_is_idempotent_per_grant(db):
    acct = _rsu_account(db)
    import_rsu_grants(db, acct, parse_merrill_rsu_text(CSV))
    # Re-import the same summary — grants matched on award_code, not duplicated.
    stats = import_rsu_grants(db, acct, parse_merrill_rsu_text(CSV))
    assert stats["grants_created"] == 0
    assert stats["grants_updated"] == 2
    assert db.execute(select(RSUGrant).where(
        RSUGrant.account_id == acct.id)).scalars().all().__len__() == 2
    # Vesting rows replaced, not stacked (3 tranches total, not 6).
    assert len(db.execute(select(RSUVest)).scalars().all()) == 3


def test_value_read_only_does_not_persist(db):
    acct = _rsu_account(db)
    import_rsu_grants(db, acct, parse_merrill_rsu_text(CSV))
    before = len(db.execute(select(AssetValuation)).scalars().all())
    value_rsu_account(db, acct, refresh_price=False, persist=False)
    after = len(db.execute(select(AssetValuation)).scalars().all())
    assert before == after
