"""Scoring a match instead of gating it, and the middle band that produces.

The old matcher applied hard gates — date, amount, description — and any one
failing meant no match, silently and permanently. A mortgage sat "overdue by
65 days" with three payments for it on the ledger because one gate (the sign)
was wrong, and "no match" is indistinguishable from "hasn't happened yet".

A score degrades instead of vetoing, and has a middle: strong evidence still
settles the schedule outright, weak evidence is still ignored, and what falls
between is recorded for a human rather than dropped.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.scheduled_match_proposal import (
    STATUS_CONFIRMED, STATUS_PENDING, STATUS_REJECTED, ScheduledMatchProposal,
)
from app.models.scheduled_payment import ScheduledPayment
from app.models.transaction import Transaction
from app.services.scheduled_matcher import (
    AUTO_THRESHOLD, PROPOSE_THRESHOLD, backfill_matches, confirm_proposal,
    pending_proposals, reject_proposal, score_match,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _mortgage(db):
    a = Account(name="Mortgage", account_type=AccountType.MORTGAGE,
                currency="USD", is_asset=False)
    db.add(a)
    db.flush()
    return a


def _sched(db, acct, description, amount, due):
    p = ScheduledPayment(
        account_id=acct.id, description=description, amount=Decimal(amount),
        currency="USD", frequency="monthly", next_due_date=due, active=True,
    )
    db.add(p)
    db.flush()
    return p


def _txn(db, acct, when, amount, description):
    t = Transaction(
        account_id=acct.id, date=datetime.combine(when, datetime.min.time()),
        description=description, amount=Decimal(amount), original_currency="USD",
    )
    db.add(t)
    db.flush()
    return t


# ── The score ────────────────────────────────────────────────────────────


def test_exact_amount_on_the_due_date_settles_regardless_of_wording(db):
    """The mortgage case. The wording shares almost nothing, but the money
    moved on the day it was due, for exactly the right amount."""
    acct = _mortgage(db)
    pmt = _sched(db, acct, "RegularPayment-(Due06/01/2026)", "-3245.24",
                 date(2026, 7, 3))
    txn = _txn(db, acct, date(2026, 7, 3), "3245.24", "Payments Irregular")

    score = score_match(db, txn, pmt, acct)

    assert score.amount == 1.0
    assert score.date == 1.0
    assert score.total >= AUTO_THRESHOLD


def test_a_wrong_amount_sinks_the_score_however_good_the_rest_is(db):
    acct = _mortgage(db)
    pmt = _sched(db, acct, "RegularPayment", "-3245.24", date(2026, 7, 3))
    txn = _txn(db, acct, date(2026, 7, 3), "1200.00", "RegularPayment")

    score = score_match(db, txn, pmt, acct)

    assert score.amount == 0.0
    assert score.total < AUTO_THRESHOLD


def test_date_evidence_decays_with_distance(db):
    acct = _mortgage(db)
    pmt = _sched(db, acct, "Rent", "-1000.00", date(2026, 7, 3))
    near = score_match(db, _txn(db, acct, date(2026, 7, 4), "1000.00", "Rent"), pmt, acct)
    mid = score_match(db, _txn(db, acct, date(2026, 7, 9), "1000.00", "Rent"), pmt, acct)
    # Beyond SCORE_DATE_WINDOW the date says nothing either way.
    far = score_match(db, _txn(db, acct, date(2026, 7, 20), "1000.00", "Rent"), pmt, acct)

    assert near.date > mid.date > far.date
    assert near.date == 1.0
    assert far.date == 0.0


def test_history_lifts_a_pairing_seen_before(db):
    """A schedule that has settled against this wording before should stop
    asking — this is what makes the queue shrink rather than repeat."""
    acct = _mortgage(db)
    pmt = _sched(db, acct, "Totally different wording", "-1000.00", date(2026, 8, 3))
    previous = _txn(db, acct, date(2026, 7, 3), "1000.00", "ACME DIRECT DEBIT")
    pmt.last_matched_txn_id = previous.id
    db.flush()
    txn = _txn(db, acct, date(2026, 8, 3), "1000.00", "ACME DIRECT DEBIT")

    score = score_match(db, txn, pmt, acct)

    assert score.history == 1.0


# ── The three bands ──────────────────────────────────────────────────────


def test_strong_evidence_still_settles_without_asking(db):
    acct = _mortgage(db)
    pmt = _sched(db, acct, "RegularPayment-(Due06/01/2026)", "-3245.24",
                 date(2026, 7, 3))
    _txn(db, acct, date(2026, 7, 3), "3245.24", "Payments Irregular")
    db.commit()

    result = backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    assert result["matched"] == 1
    assert pmt.next_due_date == date(2026, 8, 3)
    assert pending_proposals(db) == []


def test_the_middle_band_is_recorded_not_acted_on(db):
    """Right amount, but a week late and nothing alike in the wording."""
    acct = _mortgage(db)
    pmt = _sched(db, acct, "Mortgage payment", "-3245.24", date(2026, 7, 3))
    _txn(db, acct, date(2026, 7, 10), "3245.24", "ZZZZ QQQQ")
    db.commit()

    result = backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    assert result["matched"] == 0
    assert result["proposed"] == 1
    assert pmt.next_due_date == date(2026, 7, 3)      # untouched
    proposal = pending_proposals(db)[0]
    assert PROPOSE_THRESHOLD <= proposal.score < AUTO_THRESHOLD


def test_weak_evidence_is_still_ignored(db):
    acct = _mortgage(db)
    pmt = _sched(db, acct, "Mortgage payment", "-3245.24", date(2026, 7, 3))
    _txn(db, acct, date(2026, 7, 3), "12.99", "Spotify")
    db.commit()

    result = backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))

    assert result == {"matched": 0, "proposed": 0, "passes": 0}
    assert pending_proposals(db) == []


# ── Resolving a proposal ─────────────────────────────────────────────────


def _one_proposal(db):
    acct = _mortgage(db)
    pmt = _sched(db, acct, "Mortgage payment", "-3245.24", date(2026, 7, 3))
    txn = _txn(db, acct, date(2026, 7, 10), "3245.24", "ZZZZ QQQQ")
    db.commit()
    backfill_matches(db, account_id=acct.id, since=date(2026, 1, 1))
    db.commit()
    return pmt, txn, pending_proposals(db)[0]


def test_confirming_settles_the_schedule(db):
    pmt, txn, proposal = _one_proposal(db)

    assert confirm_proposal(db, proposal.id) is True

    assert pmt.last_matched_txn_id == txn.id
    assert pmt.next_due_date == date(2026, 8, 10)
    assert proposal.status == STATUS_CONFIRMED
    assert pending_proposals(db) == []


def test_rejecting_leaves_the_schedule_alone(db):
    pmt, _txn_, proposal = _one_proposal(db)
    before = pmt.next_due_date

    assert reject_proposal(db, proposal.id) is True

    assert pmt.next_due_date == before
    assert pmt.last_matched_txn_id is None
    assert proposal.status == STATUS_REJECTED
    assert pending_proposals(db) == []


def test_a_rejected_pairing_is_not_offered_again(db):
    """The point of remembering the decision."""
    pmt, _txn_, proposal = _one_proposal(db)
    reject_proposal(db, proposal.id)
    db.commit()

    result = backfill_matches(db, account_id=pmt.account_id, since=date(2026, 1, 1))

    assert result["proposed"] == 0
    assert pending_proposals(db) == []


def test_resolving_twice_is_refused(db):
    _pmt, _txn_, proposal = _one_proposal(db)
    assert confirm_proposal(db, proposal.id) is True

    assert confirm_proposal(db, proposal.id) is False
    assert reject_proposal(db, proposal.id) is False


def test_proposals_are_not_duplicated_across_runs(db):
    pmt, _txn_, _p = _one_proposal(db)
    db.commit()

    backfill_matches(db, account_id=pmt.account_id, since=date(2026, 1, 1))
    db.commit()

    assert len(db.execute(select(ScheduledMatchProposal)).scalars().all()) == 1
