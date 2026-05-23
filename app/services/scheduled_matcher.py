"""Match newly imported transactions against active scheduled payments.

Called after each import batch. For each new transaction:
  1. Find active scheduled payments for the same account.
  2. Check date is within ±DATE_WINDOW days of next_due_date.
  3. Check amount is within AMOUNT_TOLERANCE of scheduled amount.
  4. Check description similarity ≥ DESC_THRESHOLD (or skip if variable).
  5. On match: update last_matched_txn_id, last_matched_date, advance next_due_date.

Returns a dict with match counts for the import summary banner.
"""
from __future__ import annotations

from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

DATE_WINDOW       = 5    # days either side of next_due_date
AMOUNT_TOLERANCE  = 0.05 # fractional tolerance (5%)
DESC_THRESHOLD    = 0.40 # minimum description similarity


def _desc_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _advance_next_due(payment, matched_date: date) -> None:
    """Advance next_due_date by one period from matched_date."""
    from app.services.recurring_detector import _add_months
    freq = payment.frequency
    dom  = payment.day_of_month

    if freq == "weekly":
        payment.next_due_date = matched_date + timedelta(days=7)
    elif freq == "biweekly":
        payment.next_due_date = matched_date + timedelta(days=14)
    elif freq == "monthly":
        payment.next_due_date = _add_months(matched_date, 1, dom)
    elif freq == "quarterly":
        payment.next_due_date = _add_months(matched_date, 3, dom)
    elif freq == "annually":
        payment.next_due_date = _add_months(matched_date, 12, dom)
    # "once" — leave next_due_date as-is and deactivate
    if freq == "once":
        payment.active = False


def match_batch(db: "Session", import_batch_id: int) -> dict:
    """Match transactions from a specific import batch against scheduled payments.

    Returns {"matched": int, "missed": int}
    """
    from sqlalchemy import select
    from app.models.transaction import Transaction
    from app.models.scheduled_payment import ScheduledPayment

    txns = db.execute(
        select(Transaction)
        .where(Transaction.import_batch_id == import_batch_id)
        .order_by(Transaction.date)
    ).scalars().all()

    if not txns:
        return {"matched": 0, "missed": 0}

    # Load all active scheduled payments grouped by account
    payments = db.execute(
        select(ScheduledPayment)
        .where(ScheduledPayment.active.is_(True))
    ).scalars().all()

    by_account: dict[int, list] = {}
    for p in payments:
        by_account.setdefault(p.account_id, []).append(p)

    matched_count = 0

    for txn in txns:
        acct_payments = by_account.get(txn.account_id, [])
        if not acct_payments:
            continue

        txn_date = txn.date.date() if hasattr(txn.date, "date") else txn.date
        txn_amt  = float(txn.amount)

        best_payment = None
        best_score   = 0.0

        for pmt in acct_payments:
            # Date check
            date_diff = abs((txn_date - pmt.next_due_date).days)
            if date_diff > DATE_WINDOW:
                continue

            # Amount check
            pmt_amt = float(pmt.amount)
            if pmt_amt != 0:
                amt_diff = abs(txn_amt - pmt_amt) / abs(pmt_amt)
            else:
                amt_diff = abs(txn_amt)
            if amt_diff > AMOUNT_TOLERANCE:
                continue

            # Description similarity
            desc_sim = _desc_similarity(txn.description or "", pmt.description)
            if pmt.amount_type == "variable":
                desc_sim = max(desc_sim, DESC_THRESHOLD)  # relax for variable

            if desc_sim < DESC_THRESHOLD:
                continue

            # Score = description similarity × (1 - date_diff/DATE_WINDOW normalised)
            score = desc_sim * (1.0 - date_diff / (DATE_WINDOW + 1))
            if score > best_score:
                best_score   = score
                best_payment = pmt

        if best_payment is not None:
            best_payment.last_matched_txn_id = txn.id
            best_payment.last_matched_date   = txn_date
            _advance_next_due(best_payment, txn_date)
            matched_count += 1

    db.commit()

    # Count missed payments (active, past-due, unmatched this batch)
    today   = date.today()
    missed  = sum(
        1 for p in payments
        if p.active and p.next_due_date < today
        and p.last_matched_date != today  # rough proxy
    )

    return {"matched": matched_count, "missed": missed}
