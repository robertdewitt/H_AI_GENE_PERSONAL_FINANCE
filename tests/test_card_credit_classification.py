"""A credit on a card is a payment only when it says so; otherwise it is money
back from a merchant.

Every positive row on a credit card was classified as a card payment. On the
Chase card that made a $1,590 insurance refund, and 140 refunds across the
Amex cards, read as the cardholder paying the bank — hidden from spend
analysis instead of netting against the purchases they reversed.
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.enums import EconomicEventType as E
from app.models.transaction import Transaction
from app.services.event_classifier import classify_transaction, event_type_to_spend_metadata


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _card(db):
    a = Account(name="Card", account_type=AccountType.CREDIT_CARD, currency="USD", is_asset=False)
    db.add(a); db.flush()
    return a


def _txn(db, acct, amount, desc, source_type=None):
    raw = json.dumps({"Description": desc, "Type": source_type}) if source_type else None
    t = Transaction(account_id=acct.id, date=__import__("datetime").datetime(2026, 7, 6),
                    description=desc, amount=Decimal(str(amount)), original_currency="USD", raw_data=raw)
    db.add(t); db.flush()
    return t


def test_a_return_row_from_the_bank_is_a_refund(db):
    a = _card(db)
    assert classify_transaction(_txn(db, a, 1590.42, "INSURE 2 DRIVE", "Return"), a) == E.MERCHANT_REFUND


def test_a_payment_row_from_the_bank_is_a_payment(db):
    a = _card(db)
    assert classify_transaction(_txn(db, a, 250.00, "Payment Thank You Bill Pa", "Payment"), a) == E.CARD_PAYMENT_SETTLEMENT


def test_payment_wording_is_enough_without_a_row_type(db):
    """Amex exports carry no row type; the description says it."""
    a = _card(db)
    assert classify_transaction(_txn(db, a, 500.00, "PAYMENT RECEIVED - THANK YOU"), a) == E.CARD_PAYMENT_SETTLEMENT


def test_a_merchant_credit_with_no_signal_is_a_refund_not_a_payment(db):
    """The cardholder did not send British Airways money; BA sent it back."""
    a = _card(db)
    assert classify_transaction(_txn(db, a, 130.41, "BRITISH AIRWAYS UK DIRE UK"), a) == E.MERCHANT_REFUND


def test_refund_wording_wins_over_payment_wording(db):
    a = _card(db)
    assert classify_transaction(_txn(db, a, 6.50, "TFL TRAVEL REFUND TFL.GOV.UK"), a) == E.MERCHANT_REFUND


def test_an_interest_credit_nets_the_fee(db):
    a = _card(db)
    assert classify_transaction(_txn(db, a, 34.94, "CREDIT FOR INTEREST CHARGE"), a) == E.FEE


def test_an_instalment_plan_credit_is_a_card_credit(db):
    a = _card(db)
    assert classify_transaction(_txn(db, a, 678.20, "INSTALMENT PLAN"), a) == E.CARD_CREDIT


def test_a_plan_fee_charge_is_a_fee_not_a_purchase(db):
    a = _card(db)
    assert classify_transaction(_txn(db, a, -57.18, "PLAN FEE - AIRBNB * HMQK4", "Fee"), a) == E.FEE
    assert classify_transaction(_txn(db, a, -22.95, "PURCHASE INTEREST CHARGE", "Fee"), a) == E.FEE


def test_an_ordinary_charge_is_still_a_purchase(db):
    a = _card(db)
    assert classify_transaction(_txn(db, a, -80.00, "Amazon", "Sale"), a) == E.CARD_PURCHASE


def test_a_linked_transfer_still_comes_first(db):
    a = _card(db)
    t = _txn(db, a, 250.00, "INSURE 2 DRIVE", "Return")
    t.is_transfer = True
    assert classify_transaction(t, a) == E.INTERNAL_TRANSFER


def test_refunds_count_as_spend_with_the_sign_reversed():
    """Spend analysis sums signed amounts: a refund in the lifestyle bucket
    reduces spend; it must not sit in the non-spend bucket with payments."""
    assert event_type_to_spend_metadata(E.MERCHANT_REFUND.value) == ("lifestyle", True)
    assert event_type_to_spend_metadata(E.CARD_CREDIT.value) == ("lifestyle", True)
    assert event_type_to_spend_metadata(E.CARD_PAYMENT_SETTLEMENT.value)[1] is False
