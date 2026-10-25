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


# A schedule that fell far behind would otherwise list every missed period.
MAX_UNRECONCILED = 6
# card_payment.SOURCE, repeated to keep this module free of that import.
CARD_PAYMENT_SOURCE = "card_payment"


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
    # Dated after the balance's as-of date but not after today: expected to
    # have happened already, just not in the imported data yet.
    pending: bool = False
    # Dated on or before the as-of date, yet no imported transaction has
    # settled it (the matcher advances next_due_date past settled ones).
    # Shown so it can be chased up; the opening balance already reflects
    # what really happened, so it never moves the projected balance.
    unreconciled: bool = False
    # The other account of a transfer, and whether this event is the mirror
    # of a payment stored on that other account.
    counter_account_name: str | None = None
    mirrored: bool = False


@dataclass
class AccountForecast:
    account_id: int
    account_name: str
    currency: str
    opening_balance: Decimal
    # Date the opening balance reflects — the statement date or the latest
    # imported transaction. Only occurrences after it are projected.
    as_of: date | None = None
    events: list[ForecastEvent] = field(default_factory=list)
    # Occurrences up to as_of with no matching import — listed, not counted.
    unreconciled: list[ForecastEvent] = field(default_factory=list)
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


def _counter_amount(amount: Decimal, account, counter) -> Decimal:
    """The amount a transfer moves on its counter account, in that account's
    own convention.

    Asset amounts are cash-flow signed (negative = money out). Liability
    balances are an amount owed and a negative amount pays it down. A
    transfer moves net worth from one side to the other, so: take the net
    worth change on the paying side, negate it, and express it in the
    counter account's convention. Checking -2,653 → mortgage gives -2,653
    (debt down); checking -500 → savings gives +500.
    """
    delta = amount if account.is_asset else -amount
    return -delta if counter.is_asset else delta


_LEDGER_SUM_SOURCES = {"latest_balance_after", "transaction_sum", "statement_anchored"}


def _forecast_anchor(db: "Session", account, today: date) -> tuple[Decimal, date]:
    """(opening balance, as-of date) for an account's forecast.

    The balance is taken in the account's own currency — the events are, and
    mixing a base-currency opening with native-currency payments skews every
    row. The as-of date is what the balance actually reflects: a statement
    date, or for a ledger-derived balance the latest imported transaction,
    since nothing after that has been seen yet. Future-dated ledger rows
    (from the old forecast Confirm button) are left out so the opening means
    "as of" rather than "as of plus whatever was posted ahead".
    """
    from datetime import datetime
    from sqlalchemy import func, select, or_
    from app.models.transaction import Transaction
    from app.services.account_service import get_account_balance_rich
    from app.services.scheduled_confirm import CONFIRM_SOURCE

    rich = get_account_balance_rich(db, account.id, target_currency=account.currency)
    opening = Decimal(str(rich.value))
    end_of_today = datetime.combine(today, datetime.max.time())

    if rich.balance_source_used in _LEDGER_SUM_SOURCES:
        future = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.account_id == account.id,
                Transaction.date > end_of_today,
            )
        ).scalar() or 0
        future = Decimal(str(future))
        # statement_anchored subtracts ledger rows on a liability.
        opening -= -future if (rich.balance_source_used == "statement_anchored"
                               and not account.is_asset) else future

    as_of = rich.balance_as_of.date() if rich.balance_as_of else today
    if as_of >= today:
        # Computed from the ledger "now" — it is only as fresh as the last
        # real (imported or hand-entered) row.
        last_real = db.execute(
            select(func.max(Transaction.date)).where(
                Transaction.account_id == account.id,
                Transaction.date <= end_of_today,
                or_(Transaction.raw_data.is_(None),
                    ~Transaction.raw_data.contains(CONFIRM_SOURCE)),
            )
        ).scalar()
        as_of = last_real.date() if last_real else today
    return opening, min(as_of, today)


def build_forecast(
    db: "Session",
    months: int = 3,
    account_ids: "set[int] | None" = None,
) -> ForecastResult:
    """Project active scheduled payments forward for `months` months.

    When ``account_ids`` is given, only those accounts are forecast — used by
    the per-account forecast on the account detail page. A payment with a
    counter account appears in both accounts' forecasts.

    Each account starts from its balance as of the latest data it has, and
    only occurrences after that date are projected: earlier ones are already
    in the balance (or were missed), so replaying them would count them twice.
    """
    from sqlalchemy import or_, select
    from app.models.account import Account
    from app.models.scheduled_payment import ScheduledPayment

    today      = date.today()
    from app.services.recurring_detector import _add_months
    end_date = _add_months(today, months)

    _q = select(ScheduledPayment).where(ScheduledPayment.active.is_(True))
    if account_ids is not None:
        _q = _q.where(or_(
            ScheduledPayment.account_id.in_(account_ids),
            ScheduledPayment.counter_account_id.in_(account_ids),
        ))
    payments = db.execute(
        _q.order_by(ScheduledPayment.next_due_date)
    ).scalars().all()

    if not payments:
        return ForecastResult(accounts=[], all_events=[], start_date=today, end_date=end_date)

    acct_ids = {p.account_id for p in payments} | {
        p.counter_account_id for p in payments if p.counter_account_id
    }
    accounts_map = {
        a.id: a for a in db.execute(
            select(Account).where(Account.id.in_(acct_ids))
        ).scalars().all()
    }
    wanted = acct_ids if account_ids is None else (acct_ids & set(account_ids))
    anchors = {
        aid: _forecast_anchor(db, accounts_map[aid], today)
        for aid in wanted if aid in accounts_map
    }

    acct_events: dict[int, list[ForecastEvent]] = {aid: [] for aid in anchors}
    unreconciled: dict[int, list[ForecastEvent]] = {aid: [] for aid in anchors}

    for pmt in payments:
        acct = accounts_map.get(pmt.account_id)
        if acct is None:
            continue
        counter = accounts_map.get(pmt.counter_account_id) if pmt.counter_account_id else None
        currency = pmt.currency or acct.currency
        # A cross-currency transfer would need an FX leg; saving one is
        # refused, so a stray legacy row simply doesn't mirror.
        if counter is not None and counter.currency != acct.currency:
            counter = None

        # Which sides of this payment are being forecast here.
        sides = []
        if pmt.account_id in anchors:
            sides.append((acct, counter, False))
        if counter is not None and counter.id in anchors:
            sides.append((counter, acct, True))
        if not sides:
            continue

        # For variable-amount streams (salary, credit-card bills) the stored
        # amount is just an anchor — project using a 6-month trailing mean of
        # actual matching transactions, with the stored amount as fallback.
        if (pmt.amount_type or "fixed") == "variable":
            forecast_amount = _trailing_avg_amount(
                db, pmt, fallback=pmt.amount,
            )
        else:
            forecast_amount = pmt.amount

        for side, other, mirrored in sides:
            amount = _counter_amount(forecast_amount, acct, side) if mirrored else forecast_amount
            as_of = anchors[side.id][1]
            d = pmt.next_due_date
            missed = 0
            while d <= end_date:
                if pmt.end_date and d > pmt.end_date:
                    break
                past = d <= as_of
                # Matching only ever settles the side the payment is stored
                # on, so a mirror can't tell whether its past ones landed.
                if not past or (not mirrored and missed < MAX_UNRECONCILED):
                    missed += past
                    (unreconciled if past else acct_events)[side.id].append(ForecastEvent(
                        date=d,
                        account_id=side.id,
                        account_name=side.name,
                        description=pmt.description,
                        amount=amount,
                        currency=currency,
                        scheduled_payment_id=pmt.id,
                        frequency=pmt.frequency,
                        pending=not past and d <= today,
                        unreconciled=past,
                        counter_account_name=other.name if other else None,
                        mirrored=mirrored,
                    ))
                if pmt.frequency == "once":
                    break
                # A card statement payment is only known for the current
                # statement. Later months stay on the paying account as an
                # estimate of cash going out, but repeating them on the card
                # would pay off spending the forecast can't see.
                if mirrored and pmt.source == CARD_PAYMENT_SOURCE and not past:
                    break
                d = _advance_date(d, pmt.frequency, pmt.day_of_month)

    account_forecasts: list[AccountForecast] = []

    for acct_id, events in acct_events.items():
        acct = accounts_map[acct_id]
        opening, as_of = anchors[acct_id]
        events.sort(key=lambda e: e.date)

        af = AccountForecast(
            account_id=acct_id,
            account_name=acct.name,
            currency=acct.currency,
            opening_balance=opening,
            as_of=as_of,
            events=events,
            unreconciled=sorted(unreconciled[acct_id], key=lambda e: e.date),
        )

        running = opening
        af.daily_balance.append((as_of, running))

        seen_dates: dict[date, Decimal] = {}
        for evt in events:
            running += evt.amount
            seen_dates[evt.date] = running

        for d in sorted(seen_dates):
            bal = seen_dates[d]
            af.daily_balance.append((d, bal))
            # A liability's balance is the amount owed, so below zero means
            # in credit — only an asset account can run short.
            if acct.is_asset and bal < Decimal("0.00"):
                af.overdraft_dates.append(d)

        account_forecasts.append(af)

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
