"""Monthly interest accrual for interest-bearing accounts.

Posts one interest transaction per calendar month on the outstanding balance,
from a start month through a cutoff. Idempotent — months that already have an
accrual are skipped, so it is safe to run repeatedly (and to backfill history).

Interest grows the balance away from zero: a positive balance (a receivable /
money owed to you, e.g. a personal loan you made) accrues positive interest; a
negative balance (money you owe) accrues negative interest. Each month's
interest is added before the next month is computed, i.e. monthly compounding.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING

from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models.account import Account
    from app.models.transaction import Transaction

INTEREST_EVENT = "interest_accrual"


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _iter_months(start: date, end: date):
    """Yield (year, month) from start's month through end's month inclusive."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


def accrue_interest(
    db: "Session",
    account: "Account",
    start: date | None = None,
    through: date | None = None,
) -> list["Transaction"]:
    """Post monthly interest for ``account`` from ``start`` through ``through``.

    ``start`` defaults to the account's earliest transaction month; ``through``
    defaults to today. Returns the transactions created.
    """
    from app.models.transaction import Transaction

    rate = float(account.interest_rate or 0.0)
    if rate <= 0:
        return []

    through = through or date.today()

    first_txn_date = db.execute(
        select(func.min(Transaction.date)).where(
            Transaction.account_id == account.id
        )
    ).scalar()
    if start is None:
        if first_txn_date is None:
            return []
        start = first_txn_date.date() if isinstance(first_txn_date, datetime) else first_txn_date

    # Months that already carry an accrual — skip them.
    existing = db.execute(
        select(Transaction.date).where(
            Transaction.account_id == account.id,
            Transaction.event_type == INTEREST_EVENT,
        )
    ).scalars().all()
    done = {(d.year, d.month) for d in existing}

    monthly_rate = Decimal(str(rate)) / Decimal("12")
    ccy = account.currency or "USD"
    created: list[Transaction] = []

    for year, month in _iter_months(start, through):
        if (year, month) in done:
            continue
        me = _month_end(year, month)
        if me > through:
            break

        # Outstanding balance as of month end, including any interest already
        # posted in earlier months (monthly compounding).
        bal = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.account_id == account.id,
                Transaction.date <= datetime(me.year, me.month, me.day, 23, 59, 59),
            )
        ).scalar() or Decimal("0")
        bal = Decimal(str(bal))
        if bal == 0:
            continue

        interest = (abs(bal) * monthly_rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if interest == 0:
            continue
        signed = interest if bal > 0 else -interest

        txn = Transaction(
            account_id=account.id,
            date=datetime(me.year, me.month, me.day),
            description=f"Interest {rate * 100:.2f}% APR",
            amount=signed,
            original_currency=ccy,
            event_type=INTEREST_EVENT,
            classification_provenance="inferred",
            classification_confidence=1.0,
        )
        db.add(txn)
        db.flush()   # so the next month's balance includes this interest
        created.append(txn)

    return created
