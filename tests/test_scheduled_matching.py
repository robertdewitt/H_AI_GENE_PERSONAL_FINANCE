"""Scheduled payments must recognise the transactions that settled them.

Two things kept a mortgage sitting "overdue by 65 days" while its ledger
plainly showed three payments made: the matcher compared raw description text
where the detector had grouped on a normalised form, and it demanded an exact
sign match on an account where the schedule and the ledger use opposite
conventions.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.scheduled_payment import ScheduledPayment
from app.models.transaction import Transaction
from app.services.scheduled_matcher import (
    _amount_satisfies,
    _desc_similarity,
    backfill_matches,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _mortgage(db):
    a = Account(name="Villa Mortgage", account_type=AccountType.MORTGAGE,
                currency="USD", is_asset=False)
    db.add(a)
    db.flush()
    return a


def _checking(db):
    a = Account(name="Checking", account_type=AccountType.CHECKING,
                currency="USD", is_asset=True)
    db.add(a)
    db.flush()
    return a


def _sched(db, acct, description, amount, next_due):
    p = ScheduledPayment(
        account_id=acct.id, description=description, amount=Decimal(amount),
        currency="USD", frequency="monthly", next_due_date=next_due, active=True,
    )
    db.add(p)
    db.flush()
    return p


def _txn(db, acct, when, amount, description):
    t = Transaction(
        account_id=acct.id,
        date=datetime.combine(when, datetime.min.time()),
        description=description, amount=Decimal(amount), original_currency="USD",
    )
    db.add(t)
    db.flush()
    return t


# ── The two blockers, in isolation ───────────────────────────────────────


def test_liability_payment_matches_despite_the_opposite_sign():
    """The schedule holds -3245.24 (cash out); the mortgage ledger records
    +3245.24 because the payment reduces what is owed."""
    assert _amount_satisfies(3245.24, -3245.24, is_liability=True) is True


def test_an_asset_account_still_demands_the_same_sign():
    """A refund must not settle a charge on a current account."""
    assert _amount_satisfies(3245.24, -3245.24, is_liability=False) is False


def test_same_sign_still_matches_on_a_liability():
    """A card purchase scheduled and charged both negative."""
    assert _amount_satisfies(-11.99, -11.99, is_liability=True) is True


def test_amount_outside_tolerance_is_rejected():
    assert _amount_satisfies(3600.00, -3245.24, is_liability=True) is False


def test_similarity_uses_the_normalised_form():
    """A statement stamps the due date into the description and rewords it
    month to month; the raw strings score 0.33, the normalised ones 0.50."""
    assert _desc_similarity(
        "Payments Irregular", "RegularPayment-(Due06/01/2026)",
    ) >= 0.40


# ── End to end ───────────────────────────────────────────────────────────


def test_backfill_walks_a_mortgage_up_to_the_present(db):
    acct = _mortgage(db)
    pmt = _sched(db, acct, "RegularPayment-(Due06/01/2026)", "-3245.24",
                 date(2026, 7, 3))
    for month in (7, 8, 9):
        _txn(db, acct, date(2026, month, 3), "3245.24", "Payments Irregular")
    db.commit()

    result = backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    assert result["matched"] == 3
    assert pmt.last_matched_date == date(2026, 9, 3)
    assert pmt.next_due_date == date(2026, 10, 3)


def test_backfill_does_not_reuse_one_transaction_twice(db):
    """Two schedules of the same amount must not both claim one payment."""
    acct = _mortgage(db)
    a = _sched(db, acct, "RegularPayment", "-3245.24", date(2026, 7, 3))
    b = _sched(db, acct, "RegularPayment copy", "-3245.24", date(2026, 7, 3))
    _txn(db, acct, date(2026, 7, 3), "3245.24", "Payments Irregular")
    db.commit()

    backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    claimed = [p.last_matched_txn_id for p in (a, b) if p.last_matched_txn_id]
    assert len(claimed) == 1


def test_backfill_leaves_an_unpaid_schedule_overdue(db):
    """Nothing on the ledger settles it, so it must stay where it is."""
    acct = _mortgage(db)
    pmt = _sched(db, acct, "RegularPayment", "-3245.24", date(2026, 7, 3))
    _txn(db, acct, date(2026, 7, 3), "77.00", "Something else entirely")
    db.commit()

    result = backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    assert result["matched"] == 0
    assert pmt.next_due_date == date(2026, 7, 3)
    assert pmt.last_matched_date is None


def test_backfill_is_idempotent(db):
    acct = _mortgage(db)
    _sched(db, acct, "RegularPayment", "-3245.24", date(2026, 7, 3))
    _txn(db, acct, date(2026, 7, 3), "3245.24", "Payments Irregular")
    db.commit()
    backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    again = backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    assert again["matched"] == 0


def test_a_refund_does_not_settle_a_charge_on_a_current_account(db):
    """The sign relaxation is for liabilities only."""
    acct = _checking(db)
    pmt = _sched(db, acct, "Gym membership", "-45.00", date(2026, 7, 3))
    _txn(db, acct, date(2026, 7, 3), "45.00", "Gym membership refund")
    db.commit()

    result = backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    assert result["matched"] == 0
    assert pmt.next_due_date == date(2026, 7, 3)


def test_matching_clears_the_overdue_badge(db):
    """next_payment_due reads the soonest active schedule — that is what the
    account page shows as "overdue by N days"."""
    from app.services.account_service import next_payment_due

    acct = _mortgage(db)
    _sched(db, acct, "RegularPayment", "-3245.24", date(2026, 7, 3))
    for month in (7, 8, 9):
        _txn(db, acct, date(2026, month, 3), "3245.24", "Payments Irregular")
    db.commit()
    assert next_payment_due(db, acct) == date(2026, 7, 3)

    backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))
    db.commit()

    assert next_payment_due(db, acct) == date(2026, 10, 3)
