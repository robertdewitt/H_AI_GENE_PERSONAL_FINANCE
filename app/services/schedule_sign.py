"""Ledger sign → scheduled-payment sign, per account.

Scheduled amounts follow one convention everywhere the forecast reads them:
cash-flow sign on an asset account, and on a liability *negative reduces what
you owe* (a payment) and *positive adds to it* (a charge). Ledgers don't
agree with each other on liabilities: a card records charges negative and
payments positive, most loan servicers record payments positive, and some
statement exports record payments negative. So a schedule built from ledger
rows has to be converted, or a card subscription lands in the forecast as a
payment.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import case, func, select

from app.models.account import Account, AccountType
from app.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def ledger_payment_sign(db: "Session", account: Account) -> int:
    """+1 if payments are positive in this account's ledger, −1 if negative.

    Most rows on a card are charges, so its payments take the minority sign;
    most rows on a loan are payments, so they take the majority sign."""
    pos, neg = db.execute(
        select(
            func.sum(case((Transaction.amount > 0, 1), else_=0)),
            func.sum(case((Transaction.amount < 0, 1), else_=0)),
        ).where(Transaction.account_id == account.id)
    ).one()
    pos, neg = pos or 0, neg or 0
    majority = 1 if pos >= neg else -1
    if account.account_type == AccountType.CREDIT_CARD:
        return -majority if (pos or neg) else 1
    return majority


def schedule_amount(db: "Session", account: Account, ledger_amount, payment_sign: int | None = None) -> Decimal:
    """The scheduled-payment amount for a ledger amount on this account."""
    amt = Decimal(str(ledger_amount))
    if account.is_asset:
        return amt
    sign = payment_sign if payment_sign is not None else ledger_payment_sign(db, account)
    return -amt * sign


def ledger_amount(db: "Session", account: Account, scheduled) -> Decimal:
    """A scheduled amount as this account's ledger shows it — what the user
    sees and types. The conversion is its own inverse, so typed amounts go
    back through ``schedule_amount``."""
    return schedule_amount(db, account, scheduled)


def from_typed(db: "Session", account_id: int, typed) -> Decimal:
    """A schedule amount typed in the account's ledger sign → stored sign."""
    account = db.get(Account, account_id)
    return schedule_amount(db, account, typed) if account is not None else Decimal(str(typed))
