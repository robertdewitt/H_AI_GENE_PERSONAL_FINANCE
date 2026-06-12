"""Tests that the recurring detector and forecast respect the UserProfile knobs.

The detector and forecast each read six tunable thresholds from
UserProfile on every run (commit 8e598bf). These tests construct a
synthetic transaction history with known cadence and amounts, then
assert detection results and forecasted amounts at:

- the documented defaults (120-day stale window, 0.85 fixed threshold,
  6-month moving-avg window, etc.); and
- at least one non-default configuration (looser min_occurrences, shorter
  moving-avg window) to prove the tunables actually influence behaviour.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.scheduled_payment import ScheduledPayment
from app.models.transaction import Transaction
from app.models.user_profile import (
    FORECAST_MOVING_AVG_MONTHS_DEFAULT,
    RECURRING_FIXED_AMT_CONSISTENCY_DEFAULT,
    RECURRING_MIN_AMT_CONSISTENCY_DEFAULT,
    RECURRING_MIN_CONFIDENCE_DEFAULT,
    RECURRING_MIN_OCCURRENCES_DEFAULT,
    RECURRING_STALE_DAYS_DEFAULT,
)
from app.services.forecast_service import _trailing_avg_amount
from app.services.recurring_detector import detect_recurring_payments
from app.services.user_profile_service import get_profile


@pytest.fixture
def db(monkeypatch):
    """Fresh in-memory DB. We also pin date.today to a known anchor so the
    stale-cutoff tests are deterministic."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


ANCHOR_DATE = date(2026, 6, 12)


def _set_today(monkeypatch, today: date):
    """Pin date.today() inside recurring_detector to a fixed value."""
    import app.services.recurring_detector as rd

    class _D(date):
        @classmethod
        def today(cls):
            return today
    monkeypatch.setattr(rd, "date", _D)


def _seed_account(db, currency="USD"):
    a = Account(name="Acc", account_type=AccountType.CHECKING,
                currency=currency, is_asset=True)
    db.add(a)
    db.flush()
    return a


def _add_txn(db, acct_id, when: date, desc: str, amount: Decimal):
    db.add(Transaction(
        account_id=acct_id,
        date=datetime(when.year, when.month, when.day),
        description=desc,
        amount=amount,
        original_currency="USD",
    ))


# ── Defaults ─────────────────────────────────────────────────────────


def test_defaults_detect_clean_monthly_fixed(db, monkeypatch):
    """A clean monthly fixed payment over ~6 months is detected as `fixed`."""
    _set_today(monkeypatch, ANCHOR_DATE)
    acct = _seed_account(db)
    for i in range(6):
        when = ANCHOR_DATE - timedelta(days=30 * i)
        _add_txn(db, acct.id, when, "NETFLIX SUBSCRIPTION", Decimal("-15.99"))
    db.commit()

    sugs = detect_recurring_payments(db)
    netflix = [s for s in sugs if "NETFLIX" in s["description"].upper()]
    assert len(netflix) == 1
    s = netflix[0]
    assert s["frequency"] == "monthly"
    assert s["amount_type"] == "fixed"
    assert s["amount"] == Decimal("-15.99")
    assert s["occurrences"] == 6


def test_defaults_skip_stale_subscription(db, monkeypatch):
    """A monthly subscription whose last occurrence is >120 days old is dropped."""
    _set_today(monkeypatch, ANCHOR_DATE)
    acct = _seed_account(db)
    # Last seen ~5 months ago, then nothing — past the 120-day default.
    last_seen = ANCHOR_DATE - timedelta(days=150)
    for i in range(6):
        when = last_seen - timedelta(days=30 * i)
        _add_txn(db, acct.id, when, "CANCELLED SAAS", Decimal("-20.00"))
    db.commit()

    sugs = detect_recurring_payments(db)
    assert not any("CANCELLED" in s["description"].upper() for s in sugs)


def test_defaults_detect_variable_salary_as_variable(db, monkeypatch):
    """A monthly inflow with varying amounts is tagged `variable`, not `fixed`."""
    _set_today(monkeypatch, ANCHOR_DATE)
    acct = _seed_account(db)
    # Wider spread — bonus months, commission swings, etc. CV well below
    # the 0.85 fixed-vs-variable threshold.
    salaries = [
        Decimal("4500.00"), Decimal("6200.00"), Decimal("5100.00"),
        Decimal("7300.00"), Decimal("4400.00"), Decimal("5900.00"),
    ]
    for i, amt in enumerate(salaries):
        when = ANCHOR_DATE - timedelta(days=30 * i)
        _add_txn(db, acct.id, when, "EMPLOYER PAYROLL", amt)
    db.commit()

    sugs = detect_recurring_payments(db)
    salary = [s for s in sugs if "PAYROLL" in s["description"].upper()]
    assert len(salary) == 1
    assert salary[0]["amount_type"] == "variable"


# ── Non-default knob ─────────────────────────────────────────────────


def test_lowered_min_occurrences_picks_up_two_occurrence_pattern(db, monkeypatch):
    """At min_occurrences=2 the detector picks up a two-month-old pair the
    default ignores. Proves the UserProfile knob is read at every call."""
    _set_today(monkeypatch, ANCHOR_DATE)
    acct = _seed_account(db)
    for i in range(2):
        when = ANCHOR_DATE - timedelta(days=30 * i)
        _add_txn(db, acct.id, when, "NEW SUBSCRIPTION", Decimal("-9.99"))
    db.commit()

    # Default: 3 occurrences required → nothing
    sugs = detect_recurring_payments(db)
    assert not any("NEW SUBSCRIPTION" in s["description"].upper() for s in sugs)

    # Loosen to 2 occurrences in the profile and re-run
    p = get_profile(db)
    p.recurring_min_occurrences = 2
    db.commit()

    sugs = detect_recurring_payments(db)
    assert any("NEW SUBSCRIPTION" in s["description"].upper() for s in sugs)


def test_shorter_moving_avg_window_changes_forecast_amount(db, monkeypatch):
    """The forecast trailing mean is computed over `forecast_moving_avg_months`.
    Reducing the window must shift the projected amount toward recent data."""
    _set_today(monkeypatch, ANCHOR_DATE)
    acct = _seed_account(db)

    # 6 months of payroll with a step change halfway: older months ≈$4k,
    # recent months ≈$6k. Default 6-month window ≈ $5k mean; 2-month
    # window ≈ $6k mean.
    older = [Decimal("4000")] * 3
    recent = [Decimal("6000")] * 3
    monthly = recent + older  # recent first; we walk i=0..5 = today..−5mo
    for i, amt in enumerate(monthly):
        when = ANCHOR_DATE - timedelta(days=30 * i)
        _add_txn(db, acct.id, when, "EMPLOYER PAYROLL", amt)

    # ScheduledPayment record that the forecast helper looks up against
    sched = ScheduledPayment(
        account_id=acct.id,
        description="EMPLOYER PAYROLL",
        amount=Decimal("5000.00"),
        amount_type="variable",
        currency="USD",
        frequency="monthly",
        next_due_date=ANCHOR_DATE + timedelta(days=30),
        source="auto_detected",
        active=True,
    )
    db.add(sched)
    db.commit()

    default_avg = _trailing_avg_amount(db, sched, fallback=Decimal("0"))
    assert default_avg == pytest.approx(Decimal("5000"), abs=Decimal("250"))

    # Now narrow the window to 2 months
    p = get_profile(db)
    p.forecast_moving_avg_months = 2
    db.commit()
    short_avg = _trailing_avg_amount(db, sched, fallback=Decimal("0"))
    assert short_avg == pytest.approx(Decimal("6000"), abs=Decimal("250"))
    assert short_avg > default_avg


def test_profile_defaults_match_module_constants(db):
    """UserProfile auto-creates with the documented defaults — guards against
    silent drift between the model defaults and the constants used by code."""
    p = get_profile(db)
    assert p.recurring_stale_days == RECURRING_STALE_DAYS_DEFAULT
    assert p.recurring_min_occurrences == RECURRING_MIN_OCCURRENCES_DEFAULT
    assert p.recurring_min_confidence == RECURRING_MIN_CONFIDENCE_DEFAULT
    assert p.recurring_min_amt_consistency == RECURRING_MIN_AMT_CONSISTENCY_DEFAULT
    assert p.recurring_fixed_amt_consistency == RECURRING_FIXED_AMT_CONSISTENCY_DEFAULT
    assert p.forecast_moving_avg_months == FORECAST_MOVING_AVG_MONTHS_DEFAULT
