"""Card payments seen only at the bank reach the card; card limits are read
from statements and turned into a utilisation figure."""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.card_payment_mirror import mirror_card_payments
from app.services.credit_utilisation import card_use, household_utilisation
from app.services.pdf_import import _extract_credit_limit


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _acct(db, name, kind, inst=None, ccy="USD", limit=None):
    a = Account(name=name, account_type=kind, institution=inst, currency=ccy,
                is_asset=kind not in (AccountType.CREDIT_CARD,), credit_limit=limit)
    db.add(a); db.flush()
    return a


def _txn(db, acct, day, amount, desc):
    t = Transaction(account_id=acct.id, date=datetime(2025, 12, day), description=desc,
                    amount=Decimal(amount), original_currency=acct.currency)
    db.add(t); db.flush()
    return t


# ── Payments seen only at the bank ────────────────────────────────────────


def test_a_bank_payment_to_a_card_is_added_to_the_card_and_linked(db):
    bank = _acct(db, "Mission Checking", AccountType.CHECKING)
    citi = _acct(db, "Citi/Advantage", AccountType.CREDIT_CARD, inst="Citi")
    out = _txn(db, bank, 12, "-99.00", "Citi Credit Card Payment")
    db.commit()

    r = mirror_card_payments(db)

    card_rows = db.execute(select(Transaction).where(Transaction.account_id == citi.id)).scalars().all()
    assert len(r.created) == 1 and [t.amount for t in card_rows] == [Decimal("99.00")]
    assert out.is_transfer and out.transfer_link_id == card_rows[0].transfer_link_id
    assert mirror_card_payments(db).created == []          # idempotent


def test_an_existing_card_payment_is_linked_not_duplicated(db):
    bank = _acct(db, "Mission Checking", AccountType.CHECKING)
    chase = _acct(db, "Chase Hyatt Visa", AccountType.CREDIT_CARD, inst="Chase")
    out = _txn(db, bank, 22, "-300.00", "Chase Credit Card")
    inbound = _txn(db, chase, 17, "300.00", "Payment Thank You")
    db.commit()

    r = mirror_card_payments(db)

    assert r.linked == [(out.id, inbound.id)] and r.created == []


def test_purchases_and_ambiguous_issuers_are_left_alone(db):
    bank = _acct(db, "Investec", AccountType.CHECKING, ccy="GBP")
    _acct(db, "Amex Platinum", AccountType.CREDIT_CARD, inst="American Express", ccy="GBP")
    _acct(db, "BA Amex", AccountType.CREDIT_CARD, inst="American Express", ccy="GBP")
    _txn(db, bank, 12, "-4000.00", "Electronic payment American Express 300000")
    _txn(db, bank, 13, "-12.00", "Card Purchase Citi Bike")
    db.commit()

    r = mirror_card_payments(db)

    assert r.created == [] and r.linked == [] and len(r.ambiguous) == 1


# ── Credit limits ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,limit", [
    ("Credit Limit\nCredit Limit $51,000\nIncludes $10,500.00 cash advance limit\nAvailable Credit Limit $50,901", 51000),
    ("Revolving Credit limit $51,000", 51000),
    ("Total Credit Line $14,000.00 Total Minimum Payment\nCash Credit Line $2,800.00", 14000),
    ("Credit Access Line $27,500\nBalance over the Credit Access Line $0.00", 27500),
    ("Credit Summary Credit Limit£ Available Credit Limit£\nAt 19October2026 35,000.00 29,788.43", 35000),
    ("Available Credit Limit $50,901\nCash Credit Line $2,800.00", None),
])
def test_credit_limit_is_read_from_the_statement(text, limit):
    assert _extract_credit_limit(text) == limit


def test_utilisation_counts_only_cards_with_a_limit(db):
    a = _acct(db, "A", AccountType.CREDIT_CARD, limit=Decimal("10000"))
    b = _acct(db, "B", AccountType.CREDIT_CARD, limit=Decimal("30000"))
    charge = _acct(db, "Charge card", AccountType.CREDIT_CARD)
    in_credit = _acct(db, "C", AccountType.CREDIT_CARD, limit=Decimal("10000"))

    u = household_utilisation(db, [
        (a, Decimal("2500")), (b, Decimal("5500")), (charge, Decimal("9000")),
        (in_credit, Decimal("-150")),
    ], "USD")

    assert (u.total_owed, u.total_limit) == (Decimal("8000"), Decimal("50000"))
    assert round(u.pct, 1) == 16.0
    assert u.no_limit == [charge]
    assert card_use(a, Decimal("2500")).pct == 25.0
