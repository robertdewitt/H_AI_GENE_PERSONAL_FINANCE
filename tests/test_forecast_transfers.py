"""Cash flow forecast: as-of cutoff, transfer mirroring, counter-account forms."""
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
from app.services.forecast_service import build_forecast
from tests.test_tenant_isolation import _cookie, two_user_app  # noqa: F401 — fixture

TODAY = date.today()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _acct(db, name, *, is_asset=True, currency="GBP", kind=AccountType.CHECKING):
    a = Account(name=name, account_type=kind, currency=currency, is_asset=is_asset)
    db.add(a)
    db.flush()
    return a


def _txn(db, acct, days_ago, amount, raw_data=None):
    db.add(Transaction(
        account_id=acct.id, date=datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time()),
        description="seed", amount=Decimal(amount), original_currency=acct.currency,
        raw_data=raw_data,
    ))
    db.flush()


def _sched(db, acct, amount, next_due, counter=None, frequency="monthly"):
    p = ScheduledPayment(
        description="Payment", amount=Decimal(amount), currency=acct.currency,
        account_id=acct.id, counter_account_id=counter.id if counter else None,
        frequency=frequency, next_due_date=next_due, source="manual", active=True,
    )
    db.add(p)
    db.flush()
    return p


def _events(db, acct, months=3):
    fc = build_forecast(db, months=months, account_ids={acct.id})
    return fc.accounts[0] if fc.accounts else None


# ── As-of cutoff ─────────────────────────────────────────────────────────


def test_stale_schedule_does_not_replay_past_occurrences(db):
    checking = _acct(db, "Checking")
    _txn(db, checking, days_ago=10, amount="1000.00")
    _sched(db, checking, "-100.00", TODAY - timedelta(days=75))

    af = _events(db, checking)
    assert af.as_of == TODAY - timedelta(days=10)
    assert af.events, "future occurrences are still projected"
    assert all(e.date > af.as_of for e in af.events)
    for e in af.events:
        assert e.pending == (e.date <= TODAY)


def test_opening_excludes_future_dated_rows_and_stays_native(db):
    checking = _acct(db, "Checking", currency="GBP")
    _txn(db, checking, days_ago=1, amount="1000.00")
    _txn(db, checking, days_ago=-30, amount="500.00",
         raw_data='{"source": "scheduled_confirm"}')
    _sched(db, checking, "-50.00", TODAY + timedelta(days=5))

    af = _events(db, checking)
    assert af.opening_balance == Decimal("1000.00")
    assert af.as_of == TODAY - timedelta(days=1)


# ── Transfer mirroring ───────────────────────────────────────────────────


def test_checking_to_mortgage_moves_both_balances_down(db):
    checking = _acct(db, "Checking")
    mortgage = _acct(db, "Mortgage", is_asset=False, kind=AccountType.MORTGAGE)
    _txn(db, checking, 0, "5000.00")
    _sched(db, checking, "-2653.00", TODAY + timedelta(days=3), counter=mortgage)

    out = _events(db, checking).events[0]
    assert (out.amount, out.mirrored, out.counter_account_name) == (Decimal("-2653.00"), False, "Mortgage")
    mirror = _events(db, mortgage).events[0]
    # Negative on a liability pays the debt down.
    assert (mirror.amount, mirror.mirrored, mirror.counter_account_name) == (Decimal("-2653.00"), True, "Checking")


def test_checking_to_savings_mirrors_as_inflow(db):
    checking = _acct(db, "Checking")
    savings = _acct(db, "Savings", kind=AccountType.SAVINGS)
    _sched(db, checking, "-500.00", TODAY + timedelta(days=3), counter=savings)

    assert _events(db, savings).events[0].amount == Decimal("500.00")


def test_schedule_stored_on_liability_mirrors_to_payer(db):
    checking = _acct(db, "Checking")
    mortgage = _acct(db, "Mortgage", is_asset=False, kind=AccountType.MORTGAGE)
    _sched(db, mortgage, "-2656.24", TODAY + timedelta(days=3), counter=checking)

    assert _events(db, checking).events[0].amount == Decimal("-2656.24")


def test_cross_currency_counter_is_not_mirrored(db):
    gbp = _acct(db, "GBP Checking", currency="GBP")
    usd = _acct(db, "USD Checking", currency="USD")
    _sched(db, gbp, "-100.00", TODAY + timedelta(days=3), counter=usd)

    assert _events(db, usd).events == []
    assert _events(db, gbp).events[0].counter_account_name is None


def test_combined_forecast_shows_each_side_once(db):
    checking = _acct(db, "Checking")
    savings = _acct(db, "Savings", kind=AccountType.SAVINGS)
    _sched(db, checking, "-500.00", TODAY + timedelta(days=3), counter=savings, frequency="once")

    fc = build_forecast(db, months=1)
    assert sorted((e.account_name, e.amount) for e in fc.all_events) == [
        ("Checking", Decimal("-500.00")), ("Savings", Decimal("500.00")),
    ]


# ── Forms: counter account validation ────────────────────────────────────


@pytest.fixture
def alice_two_accounts(two_user_app):
    client, ctx = two_user_app
    s = ctx["session"]
    checking = s.get(Account, ctx["alice_account_id"])
    savings = Account(user_id=checking.user_id, name="A-Savings", account_type=AccountType.SAVINGS,
                      currency=checking.currency, is_asset=True)
    s.add(savings)
    s.commit()
    return client, ctx, checking.id, savings.id


def _new(client, cookie, account_id, counter):
    return client.post("/scheduled/new", data={
        "description": "To savings", "amount": "-100", "account_id": account_id,
        "currency": "USD", "frequency": "monthly",
        "next_due_date": (TODAY + timedelta(days=5)).isoformat(),
        "counter_account_id": str(counter),
    }, cookies=_cookie(cookie), follow_redirects=False)


def _stored(ctx):
    s = ctx["session"]
    s.expire_all()
    return s.query(ScheduledPayment).all()


def test_new_schedule_saves_owned_counter_account(alice_two_accounts):
    client, ctx, checking_id, savings_id = alice_two_accounts
    assert _new(client, ctx["alice_cookie"], checking_id, savings_id).status_code == 303
    assert [p.counter_account_id for p in _stored(ctx)] == [savings_id]


def test_counter_account_of_another_user_is_rejected(alice_two_accounts):
    client, ctx, checking_id, _ = alice_two_accounts
    resp = _new(client, ctx["alice_cookie"], checking_id, ctx["bob_account_id"])
    assert resp.status_code == 404
    assert _stored(ctx) == []


def test_counter_account_cannot_be_the_same_account(alice_two_accounts):
    client, ctx, checking_id, _ = alice_two_accounts
    assert _new(client, ctx["alice_cookie"], checking_id, checking_id).status_code == 400
    assert _stored(ctx) == []


def test_edit_without_counter_field_keeps_it(alice_two_accounts):
    client, ctx, checking_id, savings_id = alice_two_accounts
    _new(client, ctx["alice_cookie"], checking_id, savings_id)
    pid = _stored(ctx)[0].id
    client.post(f"/scheduled/{pid}/edit", data={
        "description": "Renamed", "amount": "-100", "account_id": checking_id,
        "currency": "USD", "frequency": "monthly",
        "next_due_date": (TODAY + timedelta(days=5)).isoformat(),
    }, cookies=_cookie(ctx["alice_cookie"]), follow_redirects=False)
    p = _stored(ctx)[0]
    assert (p.description, p.counter_account_id) == ("Renamed", savings_id)


def test_only_asset_accounts_can_run_short(db):
    """Below zero on a liability means in credit, not a shortfall."""
    card = _acct(db, "Card", is_asset=False, kind=AccountType.CREDIT_CARD)
    checking = _acct(db, "Checking")
    for acct in (card, checking):
        _sched(db, acct, "-500.00", TODAY + timedelta(days=5), frequency="once")

    assert _events(db, card).overdraft_dates == []
    assert _events(db, checking).overdraft_dates == [TODAY + timedelta(days=5)]


# ── Unreconciled occurrences ─────────────────────────────────────────────


def test_unmatched_past_occurrences_are_listed_but_not_counted(db):
    checking = _acct(db, "Checking")
    _txn(db, checking, days_ago=3, amount="1000.00")             # data runs to 3 days ago
    _sched(db, checking, "3640.00", TODAY - timedelta(days=40))  # rent, never matched

    af = _events(db, checking)
    assert [e.unreconciled for e in af.unreconciled] == [True, True]
    assert all(e.date <= af.as_of for e in af.unreconciled)
    assert all(e.date > af.as_of for e in af.events)
    # The projected balance carries on from the real one, ignoring the misses.
    assert af.daily_balance[0][1] == Decimal("1000.00")
    assert af.daily_balance[1][1] == Decimal("1000.00") + Decimal("3640.00")


def test_editing_a_schedule_changes_every_unsettled_occurrence(db):
    checking = _acct(db, "Checking")
    _txn(db, checking, days_ago=3, amount="0")
    rent = _sched(db, checking, "3765.00", TODAY - timedelta(days=40))
    rent.amount = Decimal("3640.00")
    db.flush()

    af = _events(db, checking)
    assert {e.amount for e in af.unreconciled + af.events} == {Decimal("3640.00")}


def test_unreconciled_list_is_capped(db):
    checking = _acct(db, "Checking")
    _txn(db, checking, days_ago=1, amount="0")
    _sched(db, checking, "-10.00", TODAY - timedelta(days=400))

    from app.services.forecast_service import MAX_UNRECONCILED
    assert len(_events(db, checking).unreconciled) == MAX_UNRECONCILED


def test_mirrored_side_lists_no_unreconciled(db):
    """Matching only settles the stored side, so the mirror can't know."""
    checking = _acct(db, "Checking")
    savings = _acct(db, "Savings")
    for a in (checking, savings):
        _txn(db, a, days_ago=3, amount="100.00")
    _sched(db, checking, "-50.00", TODAY - timedelta(days=40), counter=savings)

    assert len(_events(db, checking).unreconciled) == 2
    assert _events(db, savings).unreconciled == []
