"""Monthly interest accrual: math, compounding, sign, and idempotency."""
import pytest
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.interest_accrual import accrue_interest, INTEREST_EVENT


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _loan(db):
    a = Account(name="Loan to Ginny", account_type=AccountType.OTHER,
                currency="USD", is_asset=True, interest_rate=0.05)
    db.add(a)
    db.flush()
    # Principal lent out: +1000 receivable on 15 Jan 2026.
    db.add(Transaction(account_id=a.id, date=datetime(2026, 1, 15),
                       description="Loan principal", amount=Decimal("1000.00"),
                       original_currency="USD"))
    db.flush()
    return a


def test_accrues_monthly_positive_interest_and_compounds(db):
    a = _loan(db)
    created = accrue_interest(db, a, start=date(2026, 1, 1), through=date(2026, 3, 31))
    # Jan, Feb, Mar → 3 accruals.
    assert len(created) == 3
    # First month interest on 1000 @ 5%/12 = 4.17 (positive → receivable grows).
    assert created[0].amount == Decimal("4.17")
    assert created[0].event_type == INTEREST_EVENT
    # Second month compounds on 1004.17 → 4.18.
    assert created[1].amount == Decimal("4.18")
    # Balance grew by the sum of interest.
    bal = db.execute(select(func.sum(Transaction.amount)).where(
        Transaction.account_id == a.id)).scalar()
    assert bal == Decimal("1000.00") + sum(t.amount for t in created)


def test_idempotent_rerun_adds_nothing(db):
    a = _loan(db)
    accrue_interest(db, a, start=date(2026, 1, 1), through=date(2026, 3, 31))
    db.flush()
    again = accrue_interest(db, a, start=date(2026, 1, 1), through=date(2026, 3, 31))
    assert again == []
    n = db.execute(select(func.count(Transaction.id)).where(
        Transaction.account_id == a.id,
        Transaction.event_type == INTEREST_EVENT)).scalar()
    assert n == 3


def test_no_rate_no_accrual(db):
    a = _loan(db)
    a.interest_rate = 0.0
    assert accrue_interest(db, a, through=date(2026, 6, 30)) == []
