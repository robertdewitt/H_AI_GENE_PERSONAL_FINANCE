"""Self-calculated interest keeps itself correct.

Interest on an account whose balance *is* its ledger (a personal loan, a
financed vehicle) is derived monthly from the rate, not imported. Two things
follow: it is not a recurring payment to predict, and a row discovered late
invalidates every accrual after it, because each month compounds on the one
before.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.enums import BalanceTruthSource
from app.models.transaction import Transaction
from app.services.interest_accrual import (
    INTEREST_EVENT,
    accrue_interest,
    accrues_own_interest,
    resync_account_interest,
    resync_all_interest_accounts,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _loan(db, rate=0.12, truth=BalanceTruthSource.TRANSACTION_SUM.value):
    a = Account(name="Loan out", account_type=AccountType.OTHER,
                currency="USD", is_asset=True, interest_rate=rate,
                balance_truth_source=truth)
    db.add(a)
    db.flush()
    return a


def _row(db, acct, when, amount, description="Payment"):
    t = Transaction(
        account_id=acct.id,
        date=datetime.combine(when, datetime.min.time()),
        description=description, amount=Decimal(amount), original_currency="USD",
    )
    db.add(t)
    db.flush()
    return t


def _accruals(db, acct):
    return db.execute(
        select(Transaction)
        .where(Transaction.account_id == acct.id,
               Transaction.event_type == INTEREST_EVENT)
        .order_by(Transaction.date)
    ).scalars().all()


# ── Which accounts derive their own interest ─────────────────────────────


def _started_loan(db, **kw):
    """A loan that is already accruing — the first accrual is a deliberate act."""
    acct = _loan(db, **kw)
    _row(db, acct, date(2026, 1, 15), "1200.00", "Initial loan")
    db.commit()
    accrue_interest(db, acct, through=date(2026, 1, 31))
    db.commit()
    return acct


def test_ledger_backed_account_that_already_accrues_keeps_going(db):
    assert accrues_own_interest(db, _started_loan(db)) is True


def test_an_account_that_never_accrued_is_not_started_automatically(db):
    """A rate on file is not consent to synthesise interest — a financed car
    whose statements already carry the charge would grow a second one."""
    acct = _loan(db)
    _row(db, acct, date(2026, 1, 15), "1200.00", "Initial loan")
    db.commit()

    assert accrues_own_interest(db, acct) is False
    assert resync_account_interest(db, acct, through=date(2026, 4, 30))["created"] == 0
    assert _accruals(db, acct) == []


def test_the_accrue_button_still_starts_one_from_scratch(db):
    """accrue_interest itself is unconditional — that is the opt-in."""
    acct = _loan(db)
    _row(db, acct, date(2026, 1, 15), "1200.00", "Initial loan")
    db.commit()

    created = accrue_interest(db, acct, through=date(2026, 3, 31))
    db.commit()

    assert len(created) == 3
    assert accrues_own_interest(db, acct) is True


def test_statement_backed_account_does_not(db):
    """A mortgage's statement already contains the lender's interest — posting
    ours as well would count it twice."""
    acct = _loan(db, truth=BalanceTruthSource.LATEST_STATEMENT.value)

    assert accrues_own_interest(db, acct) is False
    assert resync_account_interest(db, acct)["created"] == 0


def test_account_without_a_rate_does_not(db):
    assert accrues_own_interest(db, _loan(db, rate=0.0)) is False


# ── Keeping the accruals current ─────────────────────────────────────────


def test_resync_posts_the_months_that_are_missing(db):
    acct = _started_loan(db)

    result = resync_account_interest(db, acct, through=date(2026, 4, 30))

    # January is already on file from the opt-in; Feb through Apr are added.
    assert result["created"] == 3
    assert [t.date.date() for t in _accruals(db, acct)] == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30),
    ]


def test_resync_is_idempotent(db):
    acct = _started_loan(db)
    resync_account_interest(db, acct, through=date(2026, 4, 30))
    before = len(_accruals(db, acct))

    again = resync_account_interest(db, acct, through=date(2026, 4, 30))

    assert again == {"removed": 0, "created": 0, "from_month": None}
    assert len(_accruals(db, acct)) == before


def test_a_backdated_payment_rebuilds_the_months_after_it(db):
    """The case that motivated this: a repayment turns up two months late, so
    every accrual since it was charged on a balance that was too high."""
    acct = _started_loan(db)
    resync_account_interest(db, acct, through=date(2026, 5, 31))
    original = [t.amount for t in _accruals(db, acct)]

    # A 600 repayment surfaces, dated back in February.
    _row(db, acct, date(2026, 2, 10), "-600.00")
    db.commit()
    result = resync_account_interest(db, acct, through=date(2026, 5, 31))

    rebuilt = [t.amount for t in _accruals(db, acct)]
    assert result["from_month"] == "2026-02"
    assert result["removed"] == 4 and result["created"] == 4
    assert len(rebuilt) == len(original)
    # January predates the payment and is untouched; February onwards shrink.
    assert rebuilt[0] == original[0]
    assert all(new < old for new, old in zip(rebuilt[1:], original[1:]))


def test_january_accrual_is_left_alone_when_nothing_before_it_changed(db):
    acct = _started_loan(db)
    resync_account_interest(db, acct, through=date(2026, 5, 31))
    jan = _accruals(db, acct)[0]
    jan_id, jan_amount = jan.id, jan.amount

    _row(db, acct, date(2026, 3, 10), "-100.00")
    db.commit()
    resync_account_interest(db, acct, through=date(2026, 5, 31))

    survivor = _accruals(db, acct)[0]
    assert survivor.id == jan_id and survivor.amount == jan_amount


def test_a_duplicated_month_is_rebuilt(db):
    acct = _started_loan(db)
    resync_account_interest(db, acct, through=date(2026, 2, 28))
    # Post a second accrual into the same month behind the service's back.
    dupe = _row(db, acct, date(2026, 1, 31), "12.00", "Interest 12.00% APR")
    dupe.event_type = INTEREST_EVENT
    db.commit()

    result = resync_account_interest(db, acct, through=date(2026, 2, 28))

    assert result["from_month"] == "2026-01"
    months = [(t.date.year, t.date.month) for t in _accruals(db, acct)]
    assert len(months) == len(set(months))


def test_resync_all_skips_accounts_that_do_not_qualify(db):
    ledger = _started_loan(db)
    statement = _loan(db, truth=BalanceTruthSource.LATEST_STATEMENT.value)
    _row(db, statement, date(2026, 1, 15), "1200.00", "Initial loan")
    db.commit()
    started = len(_accruals(db, ledger))

    resync_all_interest_accounts(db)
    db.commit()

    assert len(_accruals(db, ledger)) > started
    assert _accruals(db, statement) == []


# ── Not a recurring payment ──────────────────────────────────────────────


def test_derived_interest_is_not_offered_as_a_recurring_payment(db):
    """It is regenerated from the rate every month; predicting it as a
    scheduled payment would double it in the forecast."""
    from app.services.recurring_detector import detect_recurring_payments

    acct = _loan(db)
    _row(db, acct, date(2025, 1, 15), "5000.00", "Initial loan")
    db.commit()
    accrue_interest(db, acct, through=date(2026, 6, 30))
    db.commit()

    descriptions = [s["description"] for s in detect_recurring_payments(db)]

    assert not any("APR" in d for d in descriptions)


def test_bank_charged_interest_is_still_detected(db):
    """Only the rows this app generates are excluded — a real INTEREST CHARGE
    on a card statement is an obligation like any other."""
    from app.services.recurring_detector import detect_recurring_payments

    card = Account(name="Card", account_type=AccountType.CREDIT_CARD,
                   currency="USD", is_asset=False)
    db.add(card)
    db.flush()
    when = date.today() - timedelta(days=210)
    for _ in range(7):
        _row(db, card, when, "-45.00", "INTEREST CHARGE")
        when += timedelta(days=30)
    db.commit()

    descriptions = [s["description"] for s in detect_recurring_payments(db)]

    assert any("INTEREST CHARGE" in d for d in descriptions)
