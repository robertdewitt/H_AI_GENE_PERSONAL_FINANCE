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

from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

DATE_WINDOW       = 5    # days either side of next_due_date
AMOUNT_TOLERANCE  = 0.05 # fractional tolerance (5%)
DESC_THRESHOLD    = 0.40 # minimum description similarity


def _desc_similarity(a: str, b: str) -> float:
    """Similarity on the same normalised form the detector groups by.

    Statements reword the same obligation month to month and stamp the due
    date into it — "RegularPayment-(Due06/01/2026)" one month, "Payments
    Irregular" the next. Comparing the raw strings scores that pair 0.33 and
    rejects it; comparing what the detector actually keyed on scores 0.50.
    """
    from app.services.recurring_detector import _normalize

    raw = SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    normalised = SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()
    return max(raw, normalised)


def _amount_satisfies(txn_amt: float, pmt_amt: float, is_liability: bool) -> bool:
    """Whether a transaction's amount settles the scheduled amount.

    Sign is checked as well as magnitude so a refund cannot satisfy a charge.
    The exception is a liability: the schedule stores a payment as negative
    cash flow, while the ledger records it positive because it reduces what
    you owe. On those accounts the opposite sign is the expected one, and
    demanding an exact sign match meant a mortgage payment never matched its
    own schedule.
    """
    if pmt_amt == 0:
        return abs(txn_amt) <= AMOUNT_TOLERANCE
    if abs(txn_amt - pmt_amt) / abs(pmt_amt) <= AMOUNT_TOLERANCE:
        return True
    if is_liability and abs(txn_amt + pmt_amt) / abs(pmt_amt) <= AMOUNT_TOLERANCE:
        return True
    return False


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

    from app.models.account import Account
    accounts = {
        a.id: a for a in db.execute(
            select(Account).where(Account.id.in_(list(by_account) or [0]))
        ).scalars().all()
    }

    matched_count = 0
    matched_payment_ids: set[int] = set()  # prevents one payment matching two txns in the same batch

    for txn in txns:
        acct_payments = by_account.get(txn.account_id, [])
        if not acct_payments:
            continue

        txn_date = txn.date.date() if hasattr(txn.date, "date") else txn.date
        txn_amt  = float(txn.amount)

        best_payment = None
        best_score   = 0.0

        for pmt in acct_payments:
            if pmt.id in matched_payment_ids:
                continue

            # Date check
            date_diff = abs((txn_date - pmt.next_due_date).days)
            if date_diff > DATE_WINDOW:
                continue

            # Amount check
            account = accounts.get(pmt.account_id)
            is_liability = account is not None and not account.is_asset
            if not _amount_satisfies(txn_amt, float(pmt.amount), is_liability):
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
            matched_payment_ids.add(best_payment.id)
            matched_count += 1

    db.commit()

    # Count missed payments: active, past-due, AND not just matched in this batch.
    today  = date.today()
    missed = sum(
        1 for p in payments
        if p.active and p.next_due_date < today
        and p.id not in matched_payment_ids
    )

    return {"matched": matched_count, "missed": missed}


def backfill_matches(
    db: "Session",
    account_id: int | None = None,
    since: date | None = None,
    max_passes: int = 24,
) -> dict:
    """Match transactions already on the ledger against their schedules.

    match_batch only ever sees one import's rows, so a payment entered by
    hand, confirmed from an account page, or imported while its schedule
    was worded differently is never recognised — the schedule sits overdue
    while the ledger plainly shows it was paid.

    Each pass settles at most one transaction per payment (a schedule is due
    once per period), so the pass repeats until nothing more matches, which
    walks a schedule that has fallen months behind back up to the present.

    Returns ``{"matched": int, "passes": int}``.
    """
    from sqlalchemy import select

    from app.models.account import Account
    from app.models.scheduled_payment import ScheduledPayment
    from app.models.transaction import Transaction

    since = since or (date.today() - timedelta(days=730))

    payment_query = select(ScheduledPayment).where(
        ScheduledPayment.active.is_(True)
    )
    if account_id is not None:
        payment_query = payment_query.where(
            ScheduledPayment.account_id == account_id
        )

    total_matched = 0
    passes = 0
    for _ in range(max_passes):
        payments = db.execute(payment_query).scalars().all()
        if not payments:
            break

        by_account: dict[int, list] = {}
        for pmt in payments:
            by_account.setdefault(pmt.account_id, []).append(pmt)

        accounts = {
            a.id: a for a in db.execute(
                select(Account).where(Account.id.in_(list(by_account)))
            ).scalars().all()
        }

        txn_query = select(Transaction).where(
            Transaction.account_id.in_(list(by_account)),
            Transaction.date >= datetime.combine(since, datetime.min.time()),
        ).order_by(Transaction.date)
        txns = db.execute(txn_query).scalars().all()

        # A transaction already recorded as the settling row for some payment
        # must not be reused for another.
        claimed = {
            p.last_matched_txn_id for p in db.execute(
                select(ScheduledPayment)
            ).scalars().all() if p.last_matched_txn_id is not None
        }

        matched_this_pass = 0
        used_payments: set[int] = set()
        for txn in txns:
            if txn.id in claimed:
                continue
            txn_date = txn.date.date() if hasattr(txn.date, "date") else txn.date
            txn_amt = float(txn.amount)

            best_payment, best_score = None, 0.0
            for pmt in by_account.get(txn.account_id, []):
                if pmt.id in used_payments:
                    continue
                date_diff = abs((txn_date - pmt.next_due_date).days)
                if date_diff > DATE_WINDOW:
                    continue
                account = accounts.get(pmt.account_id)
                is_liability = account is not None and not account.is_asset
                if not _amount_satisfies(txn_amt, float(pmt.amount), is_liability):
                    continue
                desc_sim = _desc_similarity(txn.description or "", pmt.description)
                if pmt.amount_type == "variable":
                    desc_sim = max(desc_sim, DESC_THRESHOLD)
                if desc_sim < DESC_THRESHOLD:
                    continue
                score = desc_sim * (1.0 - date_diff / (DATE_WINDOW + 1))
                if score > best_score:
                    best_score, best_payment = score, pmt

            if best_payment is not None:
                best_payment.last_matched_txn_id = txn.id
                best_payment.last_matched_date = txn_date
                _advance_next_due(best_payment, txn_date)
                used_payments.add(best_payment.id)
                claimed.add(txn.id)
                matched_this_pass += 1

        if matched_this_pass == 0:
            break
        db.flush()
        total_matched += matched_this_pass
        passes += 1

    return {"matched": total_matched, "passes": passes}
