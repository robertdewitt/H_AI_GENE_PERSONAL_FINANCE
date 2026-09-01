"""Confirming a projected scheduled payment into the account ledger.

The forecast on an account page is a projection; confirming one occurrence
turns it into a real Transaction and moves the schedule past that date. These
tests pin the parts that are easy to get wrong: the schedule must advance
exactly one period, a repeated submit must not post the row twice, and the
redirect must not become an open redirect.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.scheduled_payment import ScheduledPayment
from app.models.transaction import Transaction


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _checking(db):
    acct = Account(name="Main", account_type=AccountType.CHECKING,
                   currency="GBP", is_asset=True)
    db.add(acct)
    db.flush()
    return acct


def _payment(db, acct, **kw):
    kw.setdefault("description", "Rent")
    kw.setdefault("amount", Decimal("-1200.00"))
    kw.setdefault("frequency", "monthly")
    kw.setdefault("next_due_date", date(2026, 9, 1))
    pmt = ScheduledPayment(account_id=acct.id, currency="GBP", **kw)
    db.add(pmt)
    db.flush()
    return pmt


def _txns(db, acct):
    return db.execute(
        select(Transaction).where(Transaction.account_id == acct.id)
    ).scalars().all()


# ── Service ───────────────────────────────────────────────────────────────


def test_confirm_posts_to_ledger_and_advances_schedule(db):
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    cat = Category(name="Housing", category_type=CategoryType.EXPENSE)
    db.add(cat)
    db.flush()
    pmt = _payment(db, acct, category_id=cat.id)

    txn, created = confirm_occurrence(db, pmt, date(2026, 9, 1))

    assert created is True
    assert txn.amount == Decimal("-1200.00")
    assert txn.date.date() == date(2026, 9, 1)
    assert txn.description == "Rent"
    assert txn.category_id == cat.id
    assert txn.original_currency == "GBP"
    assert txn.event_type == "lifestyle_expense"

    assert pmt.next_due_date == date(2026, 10, 1)
    assert pmt.last_matched_txn_id == txn.id
    assert pmt.last_matched_date == date(2026, 9, 1)


def test_forecast_amount_overrides_the_stored_anchor(db):
    """A variable payment is projected from a trailing average, so the ledger
    row must use the amount the forecast displayed — not the anchor."""
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    pmt = _payment(db, acct, amount=Decimal("-90.00"), amount_type="variable")

    txn, _ = confirm_occurrence(db, pmt, date(2026, 9, 1), Decimal("-104.37"))

    assert txn.amount == Decimal("-104.37")


def test_confirming_twice_does_not_duplicate(db):
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    pmt = _payment(db, acct)

    first, created_first = confirm_occurrence(db, pmt, date(2026, 9, 1))
    db.commit()
    second, created_second = confirm_occurrence(db, pmt, date(2026, 9, 1))

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert len(_txns(db, acct)) == 1


def test_one_off_payment_is_deactivated_after_confirming(db):
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    pmt = _payment(db, acct, frequency="once")

    confirm_occurrence(db, pmt, date(2026, 9, 1))

    assert pmt.active is False


def test_back_dated_confirm_does_not_rewind_the_schedule(db):
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    pmt = _payment(db, acct, next_due_date=date(2026, 9, 1))

    confirm_occurrence(db, pmt, date(2026, 7, 1))

    assert pmt.next_due_date == date(2026, 9, 1)
    assert len(_txns(db, acct)) == 1


def test_confirming_a_later_occurrence_advances_past_it(db):
    """Skipping ahead is allowed (the UI warns) — the schedule must land one
    period after the confirmed date, not one period after the old due date."""
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    pmt = _payment(db, acct, next_due_date=date(2026, 9, 1))

    confirm_occurrence(db, pmt, date(2026, 11, 1))

    assert pmt.next_due_date == date(2026, 12, 1)


def test_confirmed_row_counts_towards_the_balance(db):
    from app.services.account_service import get_account_balance
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    pmt = _payment(db, acct, amount=Decimal("-250.00"))
    before = get_account_balance(db, acct.id, target_currency="GBP")

    confirm_occurrence(db, pmt, date.today() + timedelta(days=3))
    db.commit()

    after = get_account_balance(db, acct.id, target_currency="GBP")
    assert after == before - Decimal("250.00")


# ── Route ─────────────────────────────────────────────────────────────────


def test_confirm_route_redirects_back_to_the_account_page(db):
    from app.routers.scheduled_payments import scheduled_confirm

    acct = _checking(db)
    pmt = _payment(db, acct)

    resp = scheduled_confirm(
        pmt.id,
        occurrence_date="2026-09-01",
        amount="-1200.00",
        return_to=f"/accounts/{acct.id}?forecast_months=6#forecast",
        db=db,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/accounts/{acct.id}?forecast_months=6&confirmed=1#forecast"
    )
    assert len(_txns(db, acct)) == 1


def test_confirm_route_reports_an_already_confirmed_occurrence(db):
    from app.routers.scheduled_payments import scheduled_confirm

    acct = _checking(db)
    pmt = _payment(db, acct)
    kw = dict(occurrence_date="2026-09-01", amount="-1200.00",
              return_to=f"/accounts/{acct.id}", db=db)

    scheduled_confirm(pmt.id, **kw)
    resp = scheduled_confirm(pmt.id, **kw)

    assert "confirmed=0" in resp.headers["location"]
    assert len(_txns(db, acct)) == 1


def test_confirm_route_rejects_a_bad_date(db):
    from app.routers.scheduled_payments import scheduled_confirm

    acct = _checking(db)
    pmt = _payment(db, acct)

    resp = scheduled_confirm(
        pmt.id, occurrence_date="not-a-date", amount="-1200.00",
        return_to=f"/accounts/{acct.id}", db=db,
    )

    assert "confirm_error=1" in resp.headers["location"]
    assert _txns(db, acct) == []


@pytest.mark.parametrize("hostile", [
    "https://evil.example/steal",
    "//evil.example/steal",
    "javascript:alert(1)",
])
def test_return_to_cannot_leave_the_site(db, hostile):
    from app.routers.scheduled_payments import scheduled_confirm

    acct = _checking(db)
    pmt = _payment(db, acct)

    resp = scheduled_confirm(
        pmt.id, occurrence_date="2026-09-01", amount="-1200.00",
        return_to=hostile, db=db,
    )

    assert resp.headers["location"].startswith(f"/accounts/{acct.id}")


# ── Amend / delete from the account page ──────────────────────────────────


def test_edit_returns_to_the_account_page_and_saves(db):
    from app.routers.scheduled_payments import scheduled_update

    acct = _checking(db)
    pmt = _payment(db, acct)
    back = f"/accounts/{acct.id}?forecast_months=6#scheduled"

    resp = scheduled_update(
        pmt.id, request=None,
        description="Rent (new landlord)", amount="-1310.00",
        amount_type="fixed", currency="GBP", account_id=acct.id,
        category_id="", frequency="monthly", next_due_date="2026-10-05",
        end_date="", day_of_month="", notes="", active="on",
        return_to=back, db=db,
    )

    assert resp.headers["location"] == (
        f"/accounts/{acct.id}?forecast_months=6&saved=1#scheduled"
    )
    assert pmt.description == "Rent (new landlord)"
    assert pmt.amount == Decimal("-1310.00")
    assert pmt.next_due_date == date(2026, 10, 5)


def test_delete_from_the_account_page_tombstones_and_returns(db):
    from app.models.dismissed_scheduled_payment import DismissedScheduledPayment
    from app.routers.scheduled_payments import scheduled_delete

    acct = _checking(db)
    pmt = _payment(db, acct)
    back = f"/accounts/{acct.id}#scheduled"

    resp = scheduled_delete(pmt.id, return_to=back, db=db)

    assert resp.headers["location"] == f"/accounts/{acct.id}?deleted=1#scheduled"
    assert db.get(ScheduledPayment, pmt.id) is None
    # Tombstoned, so the detector doesn't rebuild it on the next visit.
    assert db.execute(select(DismissedScheduledPayment)).scalars().all()


def test_toggle_from_the_account_page_returns(db):
    from app.routers.scheduled_payments import scheduled_toggle

    acct = _checking(db)
    pmt = _payment(db, acct)

    resp = scheduled_toggle(pmt.id, return_to=f"/accounts/{acct.id}#scheduled", db=db)

    assert resp.headers["location"] == f"/accounts/{acct.id}#scheduled"
    assert pmt.active is False


def test_scheduled_page_still_defaults_when_no_return_to(db):
    """The /scheduled list posts no return_to — it must keep working."""
    from app.routers.scheduled_payments import scheduled_toggle

    acct = _checking(db)
    pmt = _payment(db, acct)

    resp = scheduled_toggle(pmt.id, return_to="", db=db)

    assert resp.headers["location"] == "/scheduled"


# ── Balance includes rows recorded after the bank's running-balance marker ──


def _imported_row(db, acct, when, amount, balance_after=None):
    from datetime import datetime
    txn = Transaction(
        account_id=acct.id,
        date=datetime.combine(when, datetime.min.time()),
        description="Imported", amount=Decimal(amount),
        original_currency="GBP", balance_after=balance_after,
    )
    db.add(txn)
    db.flush()
    return txn


def test_balance_adds_rows_after_the_running_balance_marker(db):
    """An import stamps balance_after on its rows and the balance is read
    from the newest one. Anything added afterwards has no marker of its own,
    so it has to be summed on top or it never reaches the balance."""
    from app.services.account_service import get_account_balance
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    _imported_row(db, acct, date(2026, 8, 1), "-40.00", balance_after=Decimal("500.00"))
    db.commit()
    assert get_account_balance(db, acct.id, target_currency="GBP") == Decimal("500.00")

    pmt = _payment(db, acct, amount=Decimal("-125.00"),
                   next_due_date=date(2026, 9, 15))
    confirm_occurrence(db, pmt, date(2026, 9, 15))
    db.commit()

    assert get_account_balance(db, acct.id, target_currency="GBP") == Decimal("375.00")


def test_marker_alone_still_reported_when_nothing_follows_it(db):
    from app.services.account_service import get_account_balance

    acct = _checking(db)
    _imported_row(db, acct, date(2026, 8, 1), "-40.00", balance_after=Decimal("500.00"))
    db.commit()

    assert get_account_balance(db, acct.id, target_currency="GBP") == Decimal("500.00")


def test_rows_carrying_their_own_marker_are_not_double_counted(db):
    """Every row of an import has balance_after; only the newest is read."""
    from app.services.account_service import get_account_balance

    acct = _checking(db)
    _imported_row(db, acct, date(2026, 8, 1), "-40.00", balance_after=Decimal("540.00"))
    _imported_row(db, acct, date(2026, 8, 2), "-40.00", balance_after=Decimal("500.00"))
    db.commit()

    assert get_account_balance(db, acct.id, target_currency="GBP") == Decimal("500.00")


def test_hybrid_balance_includes_future_dated_rows(db):
    """A statement-anchored account was clamping its delta at today, so a
    confirmed future occurrence never showed up."""
    from datetime import datetime, timedelta

    from app.models.enums import BalanceTruthSource
    from app.services.account_service import get_account_balance

    acct = _checking(db)
    acct.balance_truth_source = BalanceTruthSource.HYBRID.value
    acct.statement_balance = Decimal("1000.00")
    acct.statement_balance_as_of = datetime.combine(
        date.today() - timedelta(days=10), datetime.min.time(),
    )
    db.flush()

    _imported_row(db, acct, date.today() - timedelta(days=2), "-30.00")
    _imported_row(db, acct, date.today() + timedelta(days=5), "-100.00")
    db.commit()

    assert get_account_balance(db, acct.id, target_currency="GBP") == Decimal("870.00")


def test_hybrid_history_still_stops_at_the_requested_date(db):
    """An explicit as_of_date must keep clamping — the time series depends on it."""
    from datetime import datetime, timedelta

    from app.models.enums import BalanceTruthSource
    from app.services.account_service import get_account_balance

    acct = _checking(db)
    acct.balance_truth_source = BalanceTruthSource.HYBRID.value
    acct.statement_balance = Decimal("1000.00")
    acct.statement_balance_as_of = datetime.combine(
        date.today() - timedelta(days=10), datetime.min.time(),
    )
    db.flush()

    _imported_row(db, acct, date.today() - timedelta(days=2), "-30.00")
    _imported_row(db, acct, date.today() + timedelta(days=5), "-100.00")
    db.commit()

    as_of = datetime.combine(date.today(), datetime.min.time())
    assert get_account_balance(
        db, acct.id, as_of_date=as_of, target_currency="GBP",
    ) == Decimal("970.00")


def test_accounts_list_agrees_with_the_account_page(db):
    """The list page uses a batched query — it must reach the same number."""
    from app.services.account_service import (
        get_account_balance, get_many_account_balances_rich,
    )

    acct = _checking(db)
    _imported_row(db, acct, date(2026, 8, 1), "-40.00", balance_after=Decimal("500.00"))
    _imported_row(db, acct, date(2026, 9, 15), "-125.00")
    db.commit()

    single = get_account_balance(db, acct.id, target_currency="GBP")
    batched = get_many_account_balances_rich(
        db, accounts=[acct], target_currency="GBP",
    )[acct.id].value

    assert single == Decimal("375.00")
    assert batched == single


def test_hybrid_liability_balance_moves_the_right_way(db):
    """A statement-anchored liability mixes two conventions: statement_balance
    is a positive amount owed, its transactions are cash-flow signed. Adding
    them made a month of spending look like paying the card off."""
    from datetime import datetime, timedelta

    from app.models.account import AccountType
    from app.models.enums import BalanceTruthSource
    from app.services.account_service import get_account_balance

    card = Account(name="Card", account_type=AccountType.CREDIT_CARD,
                   currency="GBP", is_asset=False,
                   balance_truth_source=BalanceTruthSource.HYBRID.value)
    card.statement_balance = Decimal("1000.00")
    card.statement_balance_as_of = datetime.combine(
        date.today() - timedelta(days=30), datetime.min.time(),
    )
    db.add(card)
    db.flush()

    # 400 charged, 100 paid off since the statement → 1300 owed.
    for when, amount in (
        (date.today() - timedelta(days=20), "-400.00"),
        (date.today() - timedelta(days=5), "100.00"),
    ):
        db.add(Transaction(
            account_id=card.id,
            date=datetime.combine(when, datetime.min.time()),
            description="row", amount=Decimal(amount), original_currency="GBP",
        ))
    db.commit()

    assert get_account_balance(
        db, card.id, target_currency="GBP",
    ) == Decimal("1300.00")


# ── Deleting a transaction a schedule points at ──────────────────────────


def test_deleting_a_matched_transaction_does_not_hit_the_foreign_key(db):
    """scheduled_payments.last_matched_txn_id has no cascade, so deleting the
    row it points at failed with "FOREIGN KEY constraint failed" — which took
    out bulk delete for any selection containing a matched transaction."""
    from app.routers.transactions import bulk_delete
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    pmt = _payment(db, acct)
    txn, _ = confirm_occurrence(db, pmt, date(2026, 9, 1))
    db.commit()
    assert pmt.last_matched_txn_id == txn.id

    resp = bulk_delete(txn_ids=str(txn.id), return_url="/transactions", db=db)

    assert resp.status_code == 303
    assert _txns(db, acct) == []
    assert pmt.last_matched_txn_id is None
    # The schedule still records when it was last seen.
    assert pmt.last_matched_date == date(2026, 9, 1)


def test_single_delete_clears_the_pointer_too(db):
    from app.routers.transactions import transaction_delete
    from app.services.scheduled_confirm import confirm_occurrence

    acct = _checking(db)
    pmt = _payment(db, acct)
    txn, _ = confirm_occurrence(db, pmt, date(2026, 9, 1))
    db.commit()

    transaction_delete(txn.id, return_url="/transactions", db=db)

    assert pmt.last_matched_txn_id is None
    assert _txns(db, acct) == []


def test_bulk_delete_skips_ids_that_do_not_exist(db):
    from app.routers.transactions import bulk_delete

    acct = _checking(db)
    pmt = _payment(db, acct)
    from app.services.scheduled_confirm import confirm_occurrence
    txn, _ = confirm_occurrence(db, pmt, date(2026, 9, 1))
    db.commit()

    resp = bulk_delete(
        txn_ids=f"{txn.id},999999,notanumber", return_url="/transactions", db=db,
    )

    assert resp.status_code == 303
    assert _txns(db, acct) == []
