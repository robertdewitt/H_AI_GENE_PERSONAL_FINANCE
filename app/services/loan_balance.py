"""Running balance for a loan's ledger, anchored to its statements.

Loan servicers don't send a running balance with each row, so it is
reconstructed: each statement (LiabilityBalanceSnapshot, plus the account's
own statement balance) fixes the amount owed on its date, and every row
between statements moves it by its principal — the PaymentDecomposition
principal when the payment has been broken down, otherwise its full amount.
Interest doesn't reduce what's owed, so a broken-down payment lands exactly
on the next statement.

A row is computed from the nearest statement on or after it (working back),
or, after the latest statement, from that statement working forward, so a
messy period (retried or reversed payments) only affects rows within it.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models.account import Account, AccountType
from app.models.enums import PaymentComponent
from app.models.payment_decomposition import PaymentDecomposition
from app.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOAN_TYPES = (AccountType.MORTGAGE, AccountType.LOAN)
_CARRIED = re.compile(r"(carried|brought) forward|opening balance", re.I)


def _d(dt) -> date:
    return dt.date() if hasattr(dt, "date") else dt


def _anchors(db: "Session", loan: Account) -> list[tuple[date, Decimal]]:
    from app.models.snapshots import LiabilityBalanceSnapshot

    by_day: dict[date, Decimal] = {
        _d(s.as_of_date): Decimal(s.value_native)
        for s in db.execute(
            select(LiabilityBalanceSnapshot).where(LiabilityBalanceSnapshot.account_id == loan.id)
        ).scalars()
    }
    if loan.statement_balance is not None and loan.statement_balance_as_of is not None:
        by_day[_d(loan.statement_balance_as_of)] = Decimal(loan.statement_balance)
    return sorted(by_day.items())


def loan_running_balances(db: "Session", loan: Account) -> dict[int, Decimal]:
    """{transaction id: amount owed after it} for every row on a loan account
    that statements can anchor. Positive = owed, as balances are stored."""
    if loan.account_type not in LOAN_TYPES:
        return {}
    anchors = _anchors(db, loan)
    rows = db.execute(
        select(Transaction.id, Transaction.date, Transaction.amount, Transaction.description)
        .where(Transaction.account_id == loan.id)
        .order_by(Transaction.date, Transaction.id)
    ).all()
    # Statement exports carry the balance as a row of their own; it fixes the
    # amount owed rather than moving it.
    carried = {r.id for r in rows if _CARRIED.search(r.description or "")}
    for r in rows:
        if r.id in carried:
            anchors.append((_d(r.date), abs(Decimal(r.amount))))
    anchors = sorted(dict(anchors).items())
    if not anchors:
        return {}
    # Which sign a payment has here: a loan ledger is mostly payments, so the
    # usual sign (servicers differ — some record payments as credits, some as
    # debits). Rows of the other sign (interest, fees) add to what's owed.
    signs = [Decimal(r.amount) > 0 for r in rows if r.id not in carried and r.amount]
    payment_sign = Decimal(1) if signs.count(True) >= signs.count(False) else Decimal(-1)
    principal = dict(db.execute(
        select(PaymentDecomposition.transaction_id, func.sum(PaymentDecomposition.amount))
        .join(Transaction, Transaction.id == PaymentDecomposition.transaction_id)
        .where(Transaction.account_id == loan.id,
               PaymentDecomposition.component == PaymentComponent.PRINCIPAL.value)
        .group_by(PaymentDecomposition.transaction_id)
    ).all())
    # How much each row reduces what's owed.
    effect = [
        (r.id, _d(r.date),
         Decimal(0) if r.id in carried
         else Decimal(principal[r.id]) if r.id in principal
         else Decimal(r.amount) * payment_sign)
        for r in rows
    ]

    out: dict[int, Decimal] = {}
    # Each statement covers the rows since the previous one (the first covers
    # everything before it): work back from it, newest row first.
    prev_day = date.min
    for a_day, a_owed in anchors:
        owed = a_owed
        for tid, _, eff in reversed([e for e in effect if prev_day < e[1] <= a_day]):
            out[tid] = owed
            owed += eff
        prev_day = a_day
    # A carried-forward row states its own balance.
    for r in rows:
        if r.id in carried:
            out[r.id] = abs(Decimal(r.amount))
    # Rows after the latest statement: forward from it.
    last_day, owed = anchors[-1]
    for tid, day, eff in effect:
        if day > last_day:
            owed -= eff
            out[tid] = owed
    return out
