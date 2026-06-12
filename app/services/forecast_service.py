"""Cash flow forecast — project account balances forward using scheduled payments.

Returns a ForecastResult with:
  - Per-account running balance timelines
  - Combined daily cash flow events
  - Overdraft alerts (date + account + projected deficit)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass
class ForecastEvent:
    date: date
    account_id: int
    account_name: str
    description: str
    amount: Decimal          # negative = outflow
    currency: str
    scheduled_payment_id: int
    frequency: str


@dataclass
class AccountForecast:
    account_id: int
    account_name: str
    currency: str
    opening_balance: Decimal
    events: list[ForecastEvent] = field(default_factory=list)
    # Running balance checkpoints: list of (date, balance)
    daily_balance: list[tuple[date, Decimal]] = field(default_factory=list)
    overdraft_dates: list[date] = field(default_factory=list)


@dataclass
class ForecastResult:
    accounts: list[AccountForecast]
    all_events: list[ForecastEvent]   # all events sorted by date
    start_date: date
    end_date: date


def _advance_date(d: date, frequency: str, day_of_month: int | None = None) -> date:
    from app.services.recurring_detector import _add_months
    if frequency == "weekly":
        return d + timedelta(days=7)
    elif frequency == "biweekly":
        return d + timedelta(days=14)
    elif frequency == "monthly":
        return _add_months(d, 1, day_of_month)
    elif frequency == "quarterly":
        return _add_months(d, 3, day_of_month)
    elif frequency == "annually":
        return _add_months(d, 12, day_of_month)
    return d  # once — no advance


def _trailing_avg_amount(
    db: "Session",
    payment,
    fallback: Decimal,
    months: int | None = None,
) -> Decimal:
    """Mean of matching transactions over the last ``months`` months.

    Match criteria match scheduled_matcher's: same account, description
    similarity ≥ 0.40, amount sign matching the scheduled amount's sign.
    Falls back to ``fallback`` (the stored payment amount) if fewer than 2
    matching transactions are found — too few to average reliably.
    """
    from datetime import timedelta
    from difflib import SequenceMatcher
    from sqlalchemy import select
    from app.models.transaction import Transaction
    from app.services.recurring_detector import _normalize

    if months is None:
        from app.services.user_profile_service import get_profile
        from app.models.user_profile import FORECAST_MOVING_AVG_MONTHS_DEFAULT
        months = (
            get_profile(db).forecast_moving_avg_months
            or FORECAST_MOVING_AVG_MONTHS_DEFAULT
        )

    cutoff = date.today() - timedelta(days=int(months * 30.5))
    txns = db.execute(
        select(Transaction)
        .where(
            Transaction.account_id == payment.account_id,
            Transaction.date >= cutoff,
            Transaction.is_transfer.is_(False),
        )
    ).scalars().all()
    if not txns:
        return fallback

    norm_target = _normalize(payment.description or "")
    pmt_sign = 1 if (payment.amount or 0) >= 0 else -1

    amounts: list[float] = []
    for t in txns:
        amt = float(t.amount or 0)
        # Same sign as the scheduled payment (avoid mixing refunds + charges)
        if amt == 0 or (1 if amt > 0 else -1) != pmt_sign:
            continue
        desc = t.description or ""
        # Prefer normalized exact match; fall back to sequence ratio so a
        # statement-styled description ("AMEX PAYMENT THANK YOU") still
        # matches a slightly different one ("AMEX Payment - THANK YOU").
        if _normalize(desc) == norm_target:
            amounts.append(amt)
        elif SequenceMatcher(None, desc.lower(), (payment.description or "").lower()).ratio() >= 0.6:
            amounts.append(amt)

    if len(amounts) < 2:
        return fallback
    mean = sum(amounts) / len(amounts)
    return Decimal(str(round(mean, 2)))


def _get_account_balance(db: "Session", account_id: int) -> Decimal:
    """Best available current balance for the account.

    Delegates to account_service so the forecast opening balance accounts for
    post-statement transactions (hybrid mode), running-balance markers
    (balance_after), and FX — rather than blindly returning a stale
    statement_balance.
    """
    from app.services.account_service import get_account_balance
    return get_account_balance(db, account_id)


def build_forecast(db: "Session", months: int = 3) -> ForecastResult:
    """Project all active scheduled payments forward for `months` months."""
    from sqlalchemy import select
    from app.models.account import Account
    from app.models.scheduled_payment import ScheduledPayment

    today      = date.today()
    end_date   = _advance_date(today, "monthly", None)
    # Advance end_date by months
    from app.services.recurring_detector import _add_months
    end_date = _add_months(today, months)

    # Load active scheduled payments
    payments = db.execute(
        select(ScheduledPayment)
        .where(ScheduledPayment.active.is_(True))
        .order_by(ScheduledPayment.next_due_date)
    ).scalars().all()

    if not payments:
        return ForecastResult(accounts=[], all_events=[], start_date=today, end_date=end_date)

    # Load accounts for name/currency lookup
    acct_ids = {p.account_id for p in payments}
    accounts_map = {
        a.id: a for a in db.execute(
            select(Account).where(Account.id.in_(acct_ids))
        ).scalars().all()
    }

    # Build per-account event lists
    acct_events: dict[int, list[ForecastEvent]] = {aid: [] for aid in acct_ids}

    for pmt in payments:
        acct = accounts_map.get(pmt.account_id)
        acct_name = acct.name if acct else f"Account {pmt.account_id}"
        currency  = pmt.currency or (acct.currency if acct else "USD")

        # For variable-amount streams (salary, credit-card bills) the stored
        # amount is just an anchor — project using a 6-month trailing mean of
        # actual matching transactions, with the stored amount as fallback.
        if (pmt.amount_type or "fixed") == "variable":
            forecast_amount = _trailing_avg_amount(
                db, pmt, fallback=pmt.amount,
            )
        else:
            forecast_amount = pmt.amount

        # Walk from next_due_date forward, emitting one event per period
        d = pmt.next_due_date
        while d <= end_date:
            if pmt.end_date and d > pmt.end_date:
                break
            acct_events[pmt.account_id].append(ForecastEvent(
                date=d,
                account_id=pmt.account_id,
                account_name=acct_name,
                description=pmt.description,
                amount=forecast_amount,
                currency=currency,
                scheduled_payment_id=pmt.id,
                frequency=pmt.frequency,
            ))
            if pmt.frequency == "once":
                break
            d = _advance_date(d, pmt.frequency, pmt.day_of_month)

    # Build AccountForecast with running balance
    account_forecasts: list[AccountForecast] = []

    for acct_id, events in acct_events.items():
        acct = accounts_map.get(acct_id)
        currency  = acct.currency if acct else "USD"
        opening   = _get_account_balance(db, acct_id)
        events.sort(key=lambda e: e.date)

        af = AccountForecast(
            account_id=acct_id,
            account_name=acct.name if acct else f"Account {acct_id}",
            currency=currency,
            opening_balance=opening,
            events=events,
        )

        # Compute daily running balance checkpoints (one per event date)
        running = opening
        # Emit opening balance on today
        af.daily_balance.append((today, running))

        seen_dates: dict[date, Decimal] = {}
        for evt in events:
            running += evt.amount
            seen_dates[evt.date] = running

        for d in sorted(seen_dates):
            bal = seen_dates[d]
            af.daily_balance.append((d, bal))
            if bal < Decimal("0.00"):
                af.overdraft_dates.append(d)

        account_forecasts.append(af)

    # Flatten all events for the combined timeline
    all_events: list[ForecastEvent] = []
    for evts in acct_events.values():
        all_events.extend(evts)
    all_events.sort(key=lambda e: e.date)

    return ForecastResult(
        accounts=account_forecasts,
        all_events=all_events,
        start_date=today,
        end_date=end_date,
    )
