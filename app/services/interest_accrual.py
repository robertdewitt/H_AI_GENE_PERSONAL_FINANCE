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


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


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


def accrues_own_interest(db: "Session", account: "Account") -> bool:
    """Whether this app derives the account's interest itself.

    Three things have to hold, and the third is the important one:

    * a rate to charge;
    * the ledger is the balance — a statement-anchored account already has the
      lender's own interest inside the figure it reports, and posting ours on
      top would count it twice;
    * the account is *already* accruing. Having a rate on file does not mean
      the interest should be synthesised: a financed car whose statements
      already carry the interest charge would suddenly grow a second one.
      The first accrual stays a deliberate act — the Accrue interest button
      on the account page — and only then does this service keep it current.
    """
    from app.models.enums import BalanceTruthSource
    from app.models.transaction import Transaction

    if float(account.interest_rate or 0.0) <= 0:
        return False
    truth = account.balance_truth_source or BalanceTruthSource.TRANSACTION_SUM.value
    if truth not in (
        BalanceTruthSource.TRANSACTION_SUM.value,
        BalanceTruthSource.HYBRID.value,
    ):
        return False
    return db.execute(
        select(func.count()).select_from(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.event_type == INTEREST_EVENT,
        )
    ).scalar() > 0


def _stored_accruals(db: "Session", account_id: int) -> dict:
    """The accrual this app posted for each month, keyed (year, month)."""
    from app.models.transaction import Transaction

    rows = db.execute(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.event_type == INTEREST_EVENT,
        )
    ).scalars().all()
    out: dict[tuple[int, int], list] = {}
    for row in rows:
        out.setdefault((row.date.year, row.date.month), []).append(row)
    return out


def resync_account_interest(
    db: "Session", account: "Account", through: date | None = None,
) -> dict:
    """Make the account's accruals agree with the transactions it now holds.

    Each month's interest compounds on the balance the month before, so a row
    discovered late — a repayment that turns up dated two months back —
    invalidates every accrual from that month on, not just its own. Rather
    than trust the existing rows, walk the months and recompute what each one
    *should* be; at the first month that disagrees (or is missing), drop that
    accrual and everything after it and post the run again from there.

    Idempotent: on a ledger that is already correct it writes nothing. The
    rows it deletes are ones this service generated, so they are removed
    outright rather than kept for recovery — the re-post replaces them.

    Returns ``{"removed": int, "created": int, "from_month": str | None}``.
    """
    from app.models.transaction import Transaction

    unchanged = {"removed": 0, "created": 0, "from_month": None}
    if not accrues_own_interest(db, account):
        return unchanged

    through = through or date.today()
    first_txn_date = db.execute(
        select(func.min(Transaction.date)).where(
            Transaction.account_id == account.id
        )
    ).scalar()
    if first_txn_date is None:
        return unchanged
    start = (
        first_txn_date.date()
        if isinstance(first_txn_date, datetime) else first_txn_date
    )

    rate = Decimal(str(float(account.interest_rate)))
    monthly_rate = rate / Decimal("12")
    stored = _stored_accruals(db, account.id)

    bad_month: tuple[int, int] | None = None
    for year, month in _iter_months(start, through):
        month_end = _month_end(year, month)
        if month_end > through:
            break

        rows = stored.get((year, month), [])
        # More than one accrual in a month is itself wrong — an earlier bug or
        # a double run — so treat it as a mismatch and rebuild.
        if len(rows) > 1:
            bad_month = (year, month)
            break

        posted = rows[0].amount if rows else Decimal("0.00")
        # The balance this month's interest is charged on — everything up to
        # month end except the accrual being checked, which is what
        # accrue_interest saw when it posted it.
        conditions = [
            Transaction.account_id == account.id,
            Transaction.date <= datetime(
                month_end.year, month_end.month, month_end.day, 23, 59, 59,
            ),
        ]
        if rows:
            conditions.append(Transaction.id.notin_([r.id for r in rows]))
        balance_excl = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(*conditions)
        ).scalar() or Decimal("0")
        balance_excl = Decimal(str(balance_excl))

        expected = (abs(balance_excl) * monthly_rate).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )
        if balance_excl == 0 or expected == 0:
            expected = Decimal("0.00")
        elif balance_excl < 0:
            expected = -expected

        if Decimal(str(posted)) != expected:
            bad_month = (year, month)
            break

    if bad_month is None:
        return unchanged

    # Everything from the first wrong month onwards is rebuilt: later months
    # compounded on a balance that is about to change.
    cutoff = _month_start(*bad_month)
    doomed = db.execute(
        select(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.event_type == INTEREST_EVENT,
            Transaction.date >= datetime(cutoff.year, cutoff.month, cutoff.day),
        )
    ).scalars().all()
    for row in doomed:
        db.delete(row)
    db.flush()

    created = accrue_interest(db, account, start=cutoff, through=through)
    return {
        "removed": len(doomed),
        "created": len(created),
        "from_month": cutoff.strftime("%Y-%m"),
    }


def resync_all_interest_accounts(db: "Session") -> dict:
    """Run the accrual check over every account that derives its own interest."""
    from app.models.account import Account

    totals = {"accounts": 0, "removed": 0, "created": 0}
    accounts = db.execute(
        select(Account).where(Account.interest_rate.isnot(None))
    ).scalars().all()
    for account in accounts:
        if not accrues_own_interest(db, account):
            continue
        result = resync_account_interest(db, account)
        if result["created"] or result["removed"]:
            totals["accounts"] += 1
            totals["removed"] += result["removed"]
            totals["created"] += result["created"]
    return totals
