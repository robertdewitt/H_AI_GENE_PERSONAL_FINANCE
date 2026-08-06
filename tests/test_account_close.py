"""Closing an account: history preserved, payments silenced, still in totals."""
import pytest
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.scheduled_payment import ScheduledPayment
from app.models.transaction import Transaction
from app.services.account_service import (
    close_account,
    reopen_account,
    split_closed_accounts,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _loan_with_history(db):
    a = Account(
        name="Tesla Loan", account_type=AccountType.LOAN, currency="GBP",
        is_asset=False, balance_truth_source="latest_statement",
        statement_balance=Decimal("26753.60"), payment_due_date=date(2026, 8, 23),
    )
    db.add(a)
    db.flush()
    for i in range(3):
        db.add(Transaction(
            account_id=a.id, date=datetime(2025, i + 1, 15),
            description=f"Payment {i}", amount=Decimal("-525.64"),
            original_currency="GBP",
        ))
    db.add(ScheduledPayment(
        account_id=a.id, description="Black Horse Direct Debit",
        amount=Decimal("-525.64"), amount_type="fixed", currency="GBP",
        frequency="monthly", next_due_date=date(2026, 8, 23),
        source="manual", active=True,
    ))
    db.flush()
    return a


def test_close_keeps_transactions_and_silences_payments(db):
    acct = _loan_with_history(db)
    stats = close_account(db, acct, closed_at=date(2026, 6, 1), reason="Paid off")

    assert acct.is_closed is True
    assert acct.closed_at == date(2026, 6, 1)
    assert acct.closed_reason == "Paid off"
    # Every transaction survives.
    assert db.execute(select(func.count(Transaction.id)).where(
        Transaction.account_id == acct.id)).scalar() == 3
    # Scheduled payments go quiet, and no payment is left pending.
    assert stats["scheduled_deactivated"] == 1
    assert db.execute(select(ScheduledPayment).where(
        ScheduledPayment.account_id == acct.id)).scalar_one().active is False
    assert acct.payment_due_date is None
    # Balance untouched by default — closing is organisational.
    assert acct.statement_balance == Decimal("26753.60")
    assert stats["balance_zeroed"] is False


def test_close_with_zero_balance_settles_it(db):
    acct = _loan_with_history(db)
    stats = close_account(db, acct, zero_balance=True)
    assert stats["balance_zeroed"] is True
    assert acct.statement_balance == Decimal("0.00")
    assert acct.current_value == Decimal("0.00")


def test_close_defaults_to_today(db):
    acct = _loan_with_history(db)
    close_account(db, acct)
    assert acct.closed_at == date.today()


def test_reopen_clears_closure(db):
    acct = _loan_with_history(db)
    close_account(db, acct, reason="oops")
    reopen_account(db, acct)
    assert acct.is_closed is False
    assert acct.closed_at is None
    assert acct.closed_reason is None
    # Scheduled payments deliberately stay off — the user re-enables what applies.
    assert db.execute(select(ScheduledPayment).where(
        ScheduledPayment.account_id == acct.id)).scalar_one().active is False


def test_split_moves_closed_out_of_active_groups(db):
    open_acct = Account(name="Checking", account_type=AccountType.CHECKING,
                        currency="GBP", is_asset=True)
    closed_acct = Account(name="Old Loan", account_type=AccountType.LOAN,
                          currency="GBP", is_asset=False,
                          closed_at=date(2026, 6, 1))
    db.add_all([open_acct, closed_acct])
    db.flush()

    groups = {
        "Banking": [{"account": open_acct, "balance": Decimal("100")}],
        "Loans": [{"account": closed_acct, "balance": Decimal("-50")}],
    }
    open_groups, closed = split_closed_accounts(groups)

    assert "Banking" in open_groups
    # A group that empties out disappears rather than rendering headerless.
    assert "Loans" not in open_groups
    assert len(closed) == 1
    assert closed[0]["account"].name == "Old Loan"


def test_closed_account_skipped_by_stale_task(db):
    from app.services.tasks_service import get_tasks

    def _stale_titles(session):
        return [t.title for t in get_tasks(session) if "not updated" in t.title]

    acct = _loan_with_history(db)  # last txn 2025 → stale while open
    assert _stale_titles(db), "expected a stale-account task before closing"
    close_account(db, acct)
    assert _stale_titles(db) == [], "closed account should stop nagging"
