"""Variable (placeholder) schedules settle within the user's tolerance."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.scheduled_payment import ScheduledPayment
from app.models.transaction import Transaction
from app.services.scheduled_matcher import backfill_matches
from app.services.user_profile_service import get_profile


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _setup(db, amount_type, paid="13800.00"):
    acct = Account(name="Investec", account_type=AccountType.CHECKING, currency="GBP", is_asset=True)
    db.add(acct)
    db.flush()
    p = ScheduledPayment(account_id=acct.id, description="DIRECT CREDIT MLI REF", amount=Decimal("16280.26"),
                         amount_type=amount_type, currency="GBP", frequency="monthly",
                         next_due_date=date(2026, 10, 22), active=True)
    db.add(p)
    db.add(Transaction(account_id=acct.id, date=datetime(2026, 10, 22), description="DIRECT CREDIT MLI REF",
                       amount=Decimal(paid), original_currency="GBP"))
    db.commit()
    return acct, p


def test_variable_placeholder_settles_within_tolerance(db):
    acct, p = _setup(db, "estimated")                 # 15% under the placeholder
    backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))
    assert p.last_matched_date == date(2026, 10, 22)


def test_fixed_schedule_still_needs_a_close_amount(db):
    acct, p = _setup(db, "fixed")
    backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))
    assert p.last_matched_date is None


def test_outside_tolerance_does_not_settle(db):
    acct, p = _setup(db, "estimated", paid="9000.00")   # 45% under
    backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))
    assert p.last_matched_date is None


def test_tolerance_comes_from_preferences(db):
    acct, p = _setup(db, "estimated")
    get_profile(db).variable_match_tolerance_pct = 10    # now 15% is too far
    db.commit()
    backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))
    assert p.last_matched_date is None
