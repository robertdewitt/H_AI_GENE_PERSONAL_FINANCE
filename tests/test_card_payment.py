"""Credit-card statement payments scheduled from the paying account."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.snapshots import LiabilityBalanceSnapshot
from app.models.transaction import Transaction
from app.services.card_payment import _existing, sync_card_payment
from app.services.forecast_service import build_forecast
from app.services.transfer_detector import link_transfer
from tests.test_tenant_isolation import _cookie, two_user_app  # noqa: F401 — fixture

TODAY = date(2026, 10, 25)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _accounts(db):
    checking = Account(name="Investec", account_type=AccountType.CHECKING, currency="GBP", is_asset=True)
    card = Account(name="Amex", account_type=AccountType.CREDIT_CARD, currency="GBP", is_asset=False,
                   statement_balance=Decimal("4577.25"), statement_balance_as_of=datetime(2026, 10, 15),
                   payment_due_date=date(2026, 10, 28))
    db.add_all([checking, card])
    db.flush()
    return checking, card


def _paid(db, payer, card, when, amount):
    out = Transaction(account_id=payer.id, date=datetime.combine(when, datetime.min.time()),
                      description="Electronic payment Amex", amount=-Decimal(amount), original_currency="GBP")
    inc = Transaction(account_id=card.id, date=out.date, description="Payment received",
                      amount=Decimal(amount), original_currency="GBP")
    db.add_all([out, inc])
    db.flush()
    link_transfer(db, out.id, inc.id, confirmed=True)


def test_statement_total_is_paid_from_payer_the_day_before_due(db):
    checking, card = _accounts(db)
    card.payment_account_id = checking.id

    assert sync_card_payment(db, card, today=TODAY) == "scheduled"
    s = _existing(db, card)
    assert (s.account_id, s.counter_account_id) == (checking.id, card.id)
    assert s.amount == Decimal("-4577.25")
    assert s.next_due_date == date(2026, 10, 27)


def test_payment_shows_in_both_forecasts(db):
    checking, card = _accounts(db)
    card.payment_account_id = checking.id
    sync_card_payment(db, card, today=TODAY)
    db.flush()

    fc = build_forecast(db, months=1, account_ids={checking.id, card.id})
    by_acct = {af.account_id: [e for e in af.events if "Amex payment" in e.description] for af in fc.accounts}
    assert by_acct[checking.id] and by_acct[card.id]


def test_payer_inferred_from_past_payments(db):
    checking, card = _accounts(db)
    _paid(db, checking, card, date(2026, 9, 27), "4299.74")

    assert sync_card_payment(db, card, today=TODAY) == "scheduled"
    assert _existing(db, card).account_id == checking.id


def test_nothing_owed_pauses_the_schedule(db):
    checking, card = _accounts(db)
    card.payment_account_id = checking.id
    sync_card_payment(db, card, today=TODAY)
    card.statement_balance = Decimal("-1541.45")          # in credit

    assert sync_card_payment(db, card, today=TODAY) == "paused: nothing owed"
    assert _existing(db, card).active is False


def test_new_statement_updates_amount_and_date(db):
    checking, card = _accounts(db)
    card.payment_account_id = checking.id
    sync_card_payment(db, card, today=TODAY)
    card.statement_balance = Decimal("5100.00")
    card.statement_balance_as_of = datetime(2026, 11, 15)
    card.payment_due_date = date(2026, 11, 28)

    sync_card_payment(db, card, today=TODAY)
    s = _existing(db, card)
    assert (s.amount, s.next_due_date) == (Decimal("-5100.00"), date(2026, 11, 27))


def test_due_date_estimated_from_history_when_missing(db):
    checking, card = _accounts(db)
    card.payment_account_id = checking.id
    card.payment_due_date = None
    for stmt, paid in ((date(2026, 7, 19), date(2026, 8, 13)), (date(2026, 8, 19), date(2026, 9, 13))):
        db.add(LiabilityBalanceSnapshot(
            account_id=card.id, as_of_date=datetime.combine(stmt, datetime.min.time()),
            value_native=Decimal("100"), value_base=Decimal("100"), currency="GBP",
            fx_rate=1.0, source="cc_statement_pdf", confidence=1.0, stale_flag=False))
        _paid(db, checking, card, paid, "100")
    card.statement_balance_as_of = datetime(2026, 10, 15)

    assert sync_card_payment(db, card, today=TODAY) == "scheduled"
    s = _existing(db, card)
    assert s.next_due_date == date(2026, 10, 15) + timedelta(days=25)   # 25-day lag, pay day before due
    assert "estimated" in s.notes


def test_stale_statement_is_not_projected(db):
    checking, card = _accounts(db)
    card.payment_account_id = checking.id
    card.statement_balance_as_of = datetime(2026, 5, 15)
    card.payment_due_date = date(2026, 5, 28)

    assert sync_card_payment(db, card, today=TODAY) == "paused: statement out of date"


def test_edit_form_rejects_another_users_account_as_payer(two_user_app):
    client, ctx = two_user_app
    s = ctx["session"]
    card = Account(user_id=s.get(Account, ctx["alice_account_id"]).user_id, name="A-Card",
                   account_type=AccountType.CREDIT_CARD, currency="USD", is_asset=False)
    s.add(card)
    s.commit()
    form = {"name": "A-Card", "account_type": "credit_card", "currency": "USD"}

    client.post(f"/accounts/{card.id}/edit", data={**form, "payment_account_id": str(ctx["bob_account_id"])},
                cookies=_cookie(ctx["alice_cookie"]), follow_redirects=False)
    s.expire_all()
    assert s.get(Account, card.id).payment_account_id is None

    client.post(f"/accounts/{card.id}/edit", data={**form, "payment_account_id": str(ctx["alice_account_id"])},
                cookies=_cookie(ctx["alice_cookie"]), follow_redirects=False)
    s.expire_all()
    assert s.get(Account, card.id).payment_account_id == ctx["alice_account_id"]


def test_card_side_shows_only_the_current_statements_payment(db):
    """Later months repeat on the payer as a cash estimate, not on the card."""
    checking, card = _accounts(db)
    card.payment_account_id = checking.id
    sync_card_payment(db, card, today=TODAY)
    db.flush()

    fc = build_forecast(db, months=3, account_ids={checking.id, card.id})
    n = {af.account_id: sum("Amex payment" in e.description for e in af.events) for af in fc.accounts}
    assert n[card.id] == 1
    assert n[checking.id] >= 2
