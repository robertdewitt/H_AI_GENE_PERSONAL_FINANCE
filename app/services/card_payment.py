"""Schedule each credit card's statement payment from the account that pays it.

A card's payment varies with every statement, so it can't be a fixed
schedule. Instead one schedule per card is kept in step with the card's
latest statement: the total due, paid from the card's payment account the
day before the due date. It is stored on the paying account as a transfer to
the card (``counter_account_id``), so it appears in both forecasts and the
matcher can settle it against the paying account's real payment.

``sync_card_payment`` is idempotent — call it whenever the statement, due
date or payment account changes.
"""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.models.account import Account, AccountType
from app.models.scheduled_payment import ScheduledPayment
from app.models.transaction import Transaction
from app.models.transfer_link import TransferLink

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SOURCE = "card_payment"
# A statement whose payment date is this far gone has been superseded by one
# that hasn't been imported yet; projecting it would only mislead.
STALE_AFTER = timedelta(days=31)


def _payments_into(db: "Session", card: Account):
    """(payer account id, payment date) for confirmed transfers into the card."""
    out_t, in_t = aliased(Transaction), aliased(Transaction)
    return db.execute(
        select(out_t.account_id, out_t.date)
        .select_from(TransferLink)
        .join(out_t, out_t.id == TransferLink.from_transaction_id)
        .join(in_t, in_t.id == TransferLink.to_transaction_id)
        .where(in_t.account_id == card.id, TransferLink.confirmed_by_user.is_(True))
    ).all()


def payment_account(db: "Session", card: Account) -> Account | None:
    """The card's chosen payment account, else the open, same-owner,
    same-currency asset account that has paid it most often."""
    if card.payment_account_id:
        acct = db.get(Account, card.payment_account_id)
        return acct if acct is not None and acct.closed_at is None else None
    counts = Counter(payer for payer, _ in _payments_into(db, card))
    for payer_id, _ in counts.most_common():
        acct = db.get(Account, payer_id)
        if (acct is not None and acct.closed_at is None
                and acct.account_type in (AccountType.CHECKING, AccountType.SAVINGS)
                and acct.user_id == card.user_id and acct.currency == card.currency):
            return acct
    return None


def _estimated_due_date(db: "Session", card: Account, payer: Account,
                        statement_date: date) -> date | None:
    """Due date when the statement didn't give one: the usual gap between a
    past statement and the payment that followed it, plus a day (payments are
    made the day before they're due)."""
    from app.models.snapshots import LiabilityBalanceSnapshot

    stmt_dates = sorted({
        d.date() for (d,) in db.execute(
            select(LiabilityBalanceSnapshot.as_of_date)
            .where(LiabilityBalanceSnapshot.account_id == card.id)
        ).all()
    })
    paid = sorted(d.date() for payer_id, d in _payments_into(db, card) if payer_id == payer.id)
    gaps = []
    for s in stmt_dates:
        after = [p for p in paid if s < p <= s + timedelta(days=45)]
        if after:
            gaps.append((after[0] - s).days)
    if len(gaps) < 2:
        return None
    return statement_date + timedelta(days=round(statistics.median(gaps)) + 1)


def _existing(db: "Session", card: Account) -> ScheduledPayment | None:
    return db.execute(
        select(ScheduledPayment).where(
            ScheduledPayment.source == SOURCE,
            ScheduledPayment.counter_account_id == card.id,
        ).limit(1)
    ).scalar_one_or_none()


def sync_card_payment(db: "Session", card: Account, today: date | None = None) -> str:
    """Bring the card's statement-payment schedule in line with its latest
    statement. Returns what happened, for logging and tests:
    "scheduled", "paused: <reason>" or "skipped: <reason>"."""
    if card.account_type != AccountType.CREDIT_CARD:
        return "skipped: not a credit card"
    today = today or date.today()
    sched = _existing(db, card)

    def pause(reason: str) -> str:
        if sched is not None:
            sched.active = False
        return f"paused: {reason}"

    if card.closed_at is not None:
        return pause("card closed")
    payer = payment_account(db, card)
    if payer is None:
        return pause("no payment account")
    owed = card.statement_balance
    stmt_at = card.statement_balance_as_of
    if owed is None or stmt_at is None:
        return pause("no statement")
    if owed <= 0:
        return pause("nothing owed")          # paid off or in credit

    stmt_date = stmt_at.date() if hasattr(stmt_at, "date") else stmt_at
    due, estimated = card.payment_due_date, False
    if due is None or due < stmt_date:
        due, estimated = _estimated_due_date(db, card, payer, stmt_date), True
    if due is None:
        return pause("no due date")
    pay_on = due - timedelta(days=1)
    if pay_on < today - STALE_AFTER:
        return pause("statement out of date")

    from app.services.scheduled_dismissal import is_dismissed
    description = f"{card.name} payment"
    if sched is None and is_dismissed(db, payer.id, description):
        return "skipped: deleted by user"

    sym = card.currency_symbol or ""
    notes = (f"Statement total {sym}{owed:,.2f} from the {stmt_date:%d %b %Y} statement; "
             f"due {due:%d %b %Y}{' (estimated from past payments)' if estimated else ''}.")
    if sched is None:
        sched = ScheduledPayment(source=SOURCE, frequency="monthly", confidence=0.95)
        db.add(sched)
    elif (sched.next_due_date > pay_on and sched.amount == -Decimal(owed)
          and sched.account_id == payer.id):
        # Already matched to this statement's payment and moved on a month —
        # don't drag it back. A new statement resets it.
        sched.active = True
        return "scheduled"
    sched.account_id = payer.id
    sched.counter_account_id = card.id
    sched.user_id = card.user_id
    sched.description = description
    sched.amount = -Decimal(owed)
    sched.amount_type = "estimated"
    sched.currency = card.currency
    sched.next_due_date = pay_on
    sched.day_of_month = pay_on.day
    sched.notes = notes
    sched.active = True
    return "scheduled"


def sync_all_cards(db: "Session", user_id: int | None = None) -> dict[int, str]:
    q = select(Account).where(Account.account_type == AccountType.CREDIT_CARD)
    if user_id is not None:
        q = q.where(Account.user_id == user_id)
    return {c.id: sync_card_payment(db, c) for c in db.execute(q).scalars().all()}
