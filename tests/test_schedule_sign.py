"""Scheduled amounts on liabilities: negative reduces what's owed, positive adds."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.scheduled_payment import ScheduledPayment
from app.models.transaction import Transaction
from app.services.schedule_sign import ledger_payment_sign, schedule_amount
from app.services.scheduled_matcher import backfill_matches


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _acct(db, kind, is_asset=False):
    a = Account(name=kind.value, account_type=kind, currency="GBP", is_asset=is_asset)
    db.add(a)
    db.flush()
    return a


def _t(db, acct, d, amount, desc):
    db.add(Transaction(account_id=acct.id, date=datetime.combine(d, datetime.min.time()),
                       description=desc, amount=Decimal(amount), original_currency="GBP"))
    db.flush()


def test_card_charge_adds_and_card_payment_reduces(db):
    card = _acct(db, AccountType.CREDIT_CARD)
    for i in range(5):
        _t(db, card, date(2026, 1, 1 + i), "-9.99", "TESLA")          # charges: the majority
    _t(db, card, date(2026, 1, 20), "4300.00", "PAYMENT RECEIVED")
    assert ledger_payment_sign(db, card) == 1
    assert schedule_amount(db, card, Decimal("-9.99")) == Decimal("9.99")
    assert schedule_amount(db, card, Decimal("4300.00")) == Decimal("-4300.00")


@pytest.mark.parametrize("payment", ["3245.24", "-2656.24"])
def test_loan_payment_reduces_whichever_way_the_ledger_records_it(db, payment):
    loan = _acct(db, AccountType.MORTGAGE)
    for m in range(1, 6):
        _t(db, loan, date(2026, m, 3), payment, "Payment")
    assert schedule_amount(db, loan, Decimal(payment)) == -abs(Decimal(payment))


def test_asset_amounts_are_unchanged(db):
    checking = _acct(db, AccountType.CHECKING, is_asset=True)
    assert schedule_amount(db, checking, Decimal("-23.00")) == Decimal("-23.00")


def test_detector_suggests_card_subscription_as_a_charge(db):
    from app.services.recurring_detector import detect_recurring_payments

    card = _acct(db, AccountType.CREDIT_CARD)
    today = date.today()
    for k in range(6, 0, -1):
        _t(db, card, today - timedelta(days=30 * k), "-9.99", "TESLA WEST DRAYTON")
    _t(db, card, today - timedelta(days=5), "500.00", "PAYMENT RECEIVED")
    tesla = [s for s in detect_recurring_payments(db) if "TESLA" in s["description"]]
    assert tesla and tesla[0]["amount"] == Decimal("9.99")


def test_schedule_catches_up_across_a_gap_in_the_data(db):
    """Tesla stuck at 3 Aug: no August statement, then charges on 1 Sep / 1 Oct."""
    card = _acct(db, AccountType.CREDIT_CARD)
    p = ScheduledPayment(account_id=card.id, description="TESLA WEST DRAYTON", amount=Decimal("9.99"),
                         currency="GBP", frequency="monthly", next_due_date=date(2026, 8, 3), active=True,
                         last_matched_date=date(2026, 7, 3))
    db.add(p)
    _t(db, card, date(2026, 9, 1), "-9.99", "TESLA WEST DRAYTON")
    _t(db, card, date(2026, 10, 1), "-9.99", "TESLA WEST DRAYTON")
    db.commit()

    backfill_matches(db, account_id=card.id, since=date(2026, 1, 1))
    assert p.last_matched_date == date(2026, 10, 1)
    assert p.next_due_date == date(2026, 11, 1)
