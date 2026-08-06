"""Marking a transfer to an account with no matching transaction should
create the mirror (counterpart) transaction on that account."""
import pytest
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.routers.transactions import _link_transfer


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _acct(db, name, atype=AccountType.CHECKING, currency="USD", is_asset=True):
    a = Account(name=name, account_type=atype, currency=currency, is_asset=is_asset)
    db.add(a)
    db.flush()
    return a


def test_counterpart_created_when_destination_has_no_match(db):
    src = _acct(db, "Checking")
    dst = _acct(db, "Loan to Ginny", atype=AccountType.OTHER)
    txn = Transaction(
        account_id=src.id, date=datetime(2026, 1, 10),
        description="Zelle Ginny", amount=Decimal("-100.00"),
        original_currency="USD", is_transfer=True,
    )
    db.add(txn)
    db.flush()

    _link_transfer(db, txn, dst.id)
    db.flush()

    # A mirror transaction now exists on the destination with the opposite sign.
    dst_txns = db.execute(
        select(Transaction).where(Transaction.account_id == dst.id)
    ).scalars().all()
    assert len(dst_txns) == 1
    counterpart = dst_txns[0]
    assert counterpart.amount == Decimal("100.00")
    assert counterpart.is_transfer is True
    # Both sides are linked to the same TransferLink.
    assert txn.transfer_link_id is not None
    assert counterpart.transfer_link_id == txn.transfer_link_id
    # Double-entry: the pair nets to zero.
    total = db.execute(select(func.sum(Transaction.amount))).scalar()
    assert total == Decimal("0.00")


def test_existing_match_is_linked_not_duplicated(db):
    src = _acct(db, "Checking")
    dst = _acct(db, "Savings")
    out = Transaction(
        account_id=src.id, date=datetime(2026, 1, 10),
        description="Move", amount=Decimal("-100.00"), original_currency="USD",
    )
    inn = Transaction(
        account_id=dst.id, date=datetime(2026, 1, 10),
        description="Move", amount=Decimal("100.00"), original_currency="USD",
    )
    db.add_all([out, inn])
    db.flush()

    _link_transfer(db, out, dst.id)
    db.flush()

    # No new transaction created — the existing one was linked.
    assert db.execute(
        select(func.count(Transaction.id)).where(Transaction.account_id == dst.id)
    ).scalar() == 1
    assert out.transfer_link_id is not None
    assert inn.transfer_link_id == out.transfer_link_id
