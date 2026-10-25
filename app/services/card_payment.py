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


def _statement_payment(db: "Session", card: Account, payer: Account,
                       stmt_date: date, owed: Decimal) -> Transaction | None:
    """The payment already made against this statement, if any: the latest
    of the payments after the statement date, provided together they cover
    at least half of it (people often pay a round or partial figure).

    A payment is recognised on either side — the paying account's outgoing
    row or the card's incoming one — when it is linked as a transfer into the
    card, or worded like a payment previously confirmed into this card.
    Wording is compared without digits, so reference numbers don't matter
    but two cards' payees ("American Express" / "Amercian Express Pl") stay
    distinct."""
    from app.services.recurring_detector import _normalize

    out_t, in_t = aliased(Transaction), aliased(Transaction)
    past = db.execute(
        select(out_t.account_id, out_t.description, in_t.description)
        .select_from(TransferLink)
        .join(out_t, out_t.id == TransferLink.from_transaction_id)
        .join(in_t, in_t.id == TransferLink.to_transaction_id)
        .where(in_t.account_id == card.id, TransferLink.confirmed_by_user.is_(True))
    ).all()
    payer_words = {_normalize(o) for acct, o, _ in past if acct == payer.id and o}
    card_words = {_normalize(i) for _, _, i in past if i}

    after = Transaction.date > stmt_date
    linked_in = {
        t for (t,) in db.execute(
            select(TransferLink.from_transaction_id)
            .join(in_t, in_t.id == TransferLink.to_transaction_id)
            .where(in_t.account_id == card.id)
        ).all()
    }
    payer_side = [
        t for t in db.execute(
            select(Transaction).where(Transaction.account_id == payer.id,
                                      Transaction.amount < 0, after)
        ).scalars()
        if t.id in linked_in or _normalize(t.description or "") in payer_words
    ]
    card_side = [
        t for t in db.execute(
            select(Transaction).where(Transaction.account_id == card.id,
                                      Transaction.amount > 0, after)
        ).scalars()
        if t.transfer_link_id is None and _normalize(t.description or "") in card_words
    ]
    # The same payment can appear on both sides; count each side separately
    # and take the better-evidenced one rather than adding them.
    best = max((payer_side, card_side), key=lambda ts: sum(abs(t.amount) for t in ts))
    if not best or sum(abs(t.amount) for t in best) < Decimal(owed) / 2:
        return None
    return max(best, key=lambda t: t.date)


def _statement_date(card: Account) -> date | None:
    at = card.statement_balance_as_of
    return at.date() if hasattr(at, "date") else at


def statement_dismissal_name(description: str, stmt_date: date) -> str:
    """What a deleted card payment is remembered as: tied to the statement it
    was for, so deleting it skips that statement and the next one is still
    scheduled. Readable as-is in the restore list."""
    return f"{description} [statement {stmt_date:%d %b %Y}]"


def dismissal_name(db: "Session", payment: ScheduledPayment) -> str:
    """The name to tombstone a deleted schedule under — statement-tagged for
    a card payment, the plain description for anything else."""
    if payment.source == SOURCE and payment.counter_account_id:
        card = db.get(Account, payment.counter_account_id)
        stmt_date = _statement_date(card) if card is not None else None
        if stmt_date is not None:
            return statement_dismissal_name(payment.description, stmt_date)
    return payment.description


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
    "scheduled", "paid" (this statement is already settled),
    "paused: <reason>" or "skipped: <reason>"."""
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
    if sched is None and is_dismissed(db, payer.id, statement_dismissal_name(description, stmt_date)):
        return "skipped: deleted for this statement"

    sym = card.currency_symbol or ""
    notes = (f"Statement total {sym}{owed:,.2f} from the {stmt_date:%d %b %Y} statement; "
             f"due {due:%d %b %Y}{' (estimated from past payments)' if estimated else ''}.")
    # Looked up before a new schedule joins the session, so its queries don't
    # autoflush a half-filled row.
    paid = _statement_payment(db, card, payer, stmt_date, owed)
    if paid is None and sched is not None and sched.last_matched_txn_id:
        # The matcher already settled this statement from an import.
        matched = db.get(Transaction, sched.last_matched_txn_id)
        if matched is not None and matched.date.date() > stmt_date:
            paid = matched
    if sched is None:
        sched = ScheduledPayment(source=SOURCE, frequency="monthly", confidence=0.95)
        db.add(sched)
    sched.account_id = payer.id
    sched.counter_account_id = card.id
    sched.user_id = card.user_id
    sched.description = description
    sched.amount = -Decimal(owed)
    sched.amount_type = "estimated"
    sched.currency = card.currency
    sched.day_of_month = pay_on.day
    sched.active = True
    if paid is not None:
        # This statement is settled: record the payment, and keep the next
        # month on the paying account only as an estimate until the next
        # statement arrives.
        from app.services.scheduled_matcher import _advance_next_due
        paid_on = paid.date.date() if hasattr(paid.date, "date") else paid.date
        sched.last_matched_txn_id = paid.id if paid.account_id == payer.id else None
        sched.last_matched_date = paid_on
        _advance_next_due(sched, pay_on)
        sched.notes = notes + f" Paid {sym}{abs(paid.amount):,.2f} on {paid_on:%d %b %Y}."
        return "paid"
    if sched.last_matched_date is not None and sched.last_matched_date > stmt_date:
        # A match recorded against this statement no longer stands (the
        # transfer was unlinked, say); left in place, heal_next_due would
        # skip the unpaid statement.
        sched.last_matched_txn_id = None
        sched.last_matched_date = None
    sched.next_due_date = pay_on
    sched.notes = notes
    return "scheduled"


def sync_all_cards(db: "Session", user_id: int | None = None) -> dict[int, str]:
    q = select(Account).where(Account.account_type == AccountType.CREDIT_CARD)
    if user_id is not None:
        q = q.where(Account.user_id == user_id)
    return {c.id: sync_card_payment(db, c) for c in db.execute(q).scalars().all()}
