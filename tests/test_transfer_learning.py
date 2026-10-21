"""Transfer detection learns from confirmations and dismissals."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.transfer_detector import (
    detect_transfers, dismiss_transfer_pair, link_transfer,
)

OUT_DESC = "STANDING ORDER SO0000{ref} 2653 mortgagemain Barclays"
IN_DESC = "Payment received {ref}"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _acct(db, name):
    a = Account(name=name, account_type=AccountType.CHECKING, currency="USD", is_asset=True)
    db.add(a)
    db.flush()
    return a


def _pair(db, src, dst, day, amount="2653.00", out_desc=OUT_DESC, in_desc=IN_DESC):
    out = Transaction(account_id=src.id, date=datetime(2026, day // 31 + 1, day % 31 + 1),
                      description=out_desc.format(ref=day), amount=-Decimal(amount),
                      original_currency="USD")
    inc = Transaction(account_id=dst.id, date=out.date,
                      description=in_desc.format(ref=day), amount=Decimal(amount),
                      original_currency="USD")
    db.add_all([out, inc])
    db.flush()
    return out, inc


def _candidate(db, out, inc):
    return next(c for c in detect_transfers(db)
                if (c.from_transaction_id, c.to_transaction_id) == (out.id, inc.id))


def test_confirmed_pattern_raises_confidence_of_the_next_one(db):
    checking, savings = _acct(db, "Checking"), _acct(db, "Savings")
    first = _pair(db, checking, savings, day=3)
    before = _candidate(db, *first)
    assert before.learned_note is None

    link_transfer(db, first[0].id, first[1].id, confirmed=True)
    nxt = _pair(db, checking, savings, day=40)  # new reference numbers, next month
    after = _candidate(db, *nxt)

    assert after.confidence > before.confidence
    assert after.learned_note == "Matches a transfer you confirmed before"


def test_route_history_boosts_new_descriptions_between_same_accounts(db):
    checking, savings, other = _acct(db, "Checking"), _acct(db, "Savings"), _acct(db, "Other")
    for day in (3, 10):
        o, i = _pair(db, checking, savings, day=day)
        link_transfer(db, o.id, i.id, confirmed=True)

    same_route = _pair(db, checking, savings, day=40, out_desc="Misc {ref}", in_desc="Misc {ref}")
    new_route = _pair(db, checking, other, day=50, out_desc="Misc {ref}", in_desc="Misc {ref}")

    a, b = _candidate(db, *same_route), _candidate(db, *new_route)
    assert a.confidence > b.confidence
    assert a.learned_note == "You've confirmed 2 transfers between these accounts"


def test_dismissed_pattern_lowers_confidence(db):
    checking, savings = _acct(db, "Checking"), _acct(db, "Savings")
    baseline = _candidate(db, *_pair(db, checking, savings, day=3,
                                     out_desc="Gym {ref}", in_desc="Refund {ref}"))
    dismiss_transfer_pair(db, baseline.from_transaction_id, baseline.to_transaction_id)

    again = _pair(db, checking, savings, day=40, out_desc="Gym {ref}", in_desc="Refund {ref}")
    cand = _candidate(db, *again)
    assert cand.confidence < baseline.confidence
    assert cand.learned_note == "Looks like a pair you marked Not a Transfer"


def test_history_is_keyed_by_account(db):
    """A pattern confirmed between one pair of accounts says nothing about another."""
    a1, a2, b1, b2 = (_acct(db, n) for n in ("A1", "A2", "B1", "B2"))
    o, i = _pair(db, a1, a2, day=3)
    link_transfer(db, o.id, i.id, confirmed=True)

    cand = _candidate(db, *_pair(db, b1, b2, day=40))
    assert cand.learned_note is None
