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

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

DATE_WINDOW       = 5    # days either side of next_due_date
AMOUNT_TOLERANCE  = 0.05 # fractional tolerance (5%)
DESC_THRESHOLD    = 0.40 # minimum description similarity


def _desc_similarity(a: str, b: str, db: "Session | None" = None) -> float:
    """Similarity between a transaction's wording and a schedule's.

    Statements reword the same obligation month to month and stamp the due
    date into it — "RegularPayment-(Due06/01/2026)" one month, "Payments
    Irregular" the next. Comparing the raw strings scores that pair 0.33 and
    rejects it; comparing normalised forms scores 0.50, and embeddings score
    it 0.65.

    Embeddings are used when a ``db`` is available to cache them and a local
    model is reachable. Measured against confirmed duplicate decisions they
    did not help — that is a different question — but on this one they do,
    which is why they are wired here and not there.
    """
    if db is not None:
        from app.services.embeddings import similarity

        return similarity(db, a or "", b or "")

    from app.services.text_keys import comparison_key

    raw = SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    normalised = SequenceMatcher(
        None, comparison_key(a), comparison_key(b)
    ).ratio()
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


# ── Scoring ───────────────────────────────────────────────────────────────
#
# The gates above are a blunt instrument: any one of them failing means no
# match, silently and permanently. That is how a mortgage sat "overdue by 65
# days" with three payments for it on the ledger — the sign check failed, and
# "no match" looks exactly like "hasn't happened yet".
#
# A weighted score degrades instead. One weak signal pulls the total down
# rather than vetoing, and the total decides between settling the schedule
# outright, proposing it for a human, and ignoring it.

AUTO_THRESHOLD = 0.80      # settle the schedule without asking
PROPOSE_THRESHOLD = 0.55   # surface for confirmation
SCORE_DATE_WINDOW = 12     # days beyond which date evidence is worthless

# Amount and date carry most of the weight because they are objective: the
# money either moved on the day it was due or it did not. Wording is the
# weakest signal — statements reword the same obligation month to month — so
# an exact amount on the exact due date clears the bar on its own, whatever
# the description says. History then lifts a repeat of an already-confirmed
# pairing over the line, so a schedule asks once and flows thereafter.
WEIGHTS = {"amount": 0.50, "date": 0.25, "description": 0.15, "history": 0.10}


@dataclass
class MatchScore:
    total: float
    amount: float
    date: float
    description: float
    history: float

    def as_dict(self) -> dict:
        return {
            "total": round(self.total, 3),
            "amount": round(self.amount, 3),
            "date": round(self.date, 3),
            "description": round(self.description, 3),
            "history": round(self.history, 3),
        }


def _amount_score(txn_amt: float, pmt_amt: float, is_liability: bool) -> float:
    """1.0 for an exact hit, tapering to 0 at 15% off."""
    if pmt_amt == 0:
        return 1.0 if abs(txn_amt) < 0.01 else 0.0
    direct = abs(txn_amt - pmt_amt) / abs(pmt_amt)
    # A liability records a payment with the opposite sign to the schedule —
    # see _amount_satisfies — so the flipped reading is equally valid there.
    best = min(direct, abs(txn_amt + pmt_amt) / abs(pmt_amt)) if is_liability else direct
    if best <= 0.005:
        return 1.0
    if best >= 0.15:
        return 0.0
    return 1.0 - (best - 0.005) / 0.145


def _date_score(day_gap: int) -> float:
    if day_gap <= 1:
        return 1.0
    if day_gap >= SCORE_DATE_WINDOW:
        return 0.0
    return 1.0 - (day_gap - 1) / (SCORE_DATE_WINDOW - 1)


def _history_score(db, payment, description: str) -> float:
    """Has this schedule been settled by a transaction worded like this before?

    The strongest signal available and the one a single-shot comparison
    cannot see: eleven previous months of the same wording says far more
    than any similarity ratio.
    """
    if payment.last_matched_txn_id is None:
        return 0.0
    from app.models.transaction import Transaction
    from app.services.recurring_detector import _normalize

    previous = db.get(Transaction, payment.last_matched_txn_id)
    if previous is None or not previous.description:
        return 0.0
    return 1.0 if _normalize(previous.description) == _normalize(description) else 0.0


def score_match(db, txn, payment, account) -> MatchScore:
    """How strongly this transaction looks like this scheduled payment."""
    txn_date = txn.date.date() if hasattr(txn.date, "date") else txn.date
    is_liability = account is not None and not account.is_asset

    amount = _amount_score(float(txn.amount), float(payment.amount), is_liability)
    date_gap = abs((txn_date - payment.next_due_date).days)
    date = _date_score(date_gap)

    description = _desc_similarity(
        txn.description or "", payment.description, db,
    )
    history = _history_score(db, payment, txn.description or "")

    total = (
        WEIGHTS["amount"] * amount
        + WEIGHTS["date"] * date
        + WEIGHTS["description"] * description
        + WEIGHTS["history"] * history
    )
    # Rounded so a total that is arithmetically exactly the threshold is not
    # pushed under it by binary floating point (0.45 + 0.25 + 0.10 lands on
    # 0.7999999999999999, which silently demoted an obvious match).
    return MatchScore(round(total, 6), amount, date, description, history)


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

    Strong evidence settles a schedule outright; the band below that is
    recorded as a proposal for a human rather than discarded.

    Returns ``{"matched": int, "proposed": int, "passes": int}``.
    """
    from sqlalchemy import select

    from app.models.account import Account
    from app.models.scheduled_payment import ScheduledPayment
    from app.models.transaction import Transaction

    since = since or (date.today() - timedelta(days=730))
    proposed = 0

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

            best_payment, best = None, None
            for pmt in by_account.get(txn.account_id, []):
                if pmt.id in used_payments:
                    continue
                # A period already settled is not a candidate again. Without
                # this, every historical occurrence of a long-running direct
                # debit scores 1.0 on amount, wording and history and floods
                # the queue with payments made years ago.
                if pmt.last_matched_date and txn_date <= pmt.last_matched_date:
                    continue
                account = accounts.get(pmt.account_id)
                scored = score_match(db, txn, pmt, account)
                if best is None or scored.total > best.total:
                    best, best_payment = scored, pmt

            if best_payment is None or best.total < PROPOSE_THRESHOLD:
                continue
            # An occurrence is defined by when it fell due, so a transaction
            # outside the date window is not evidence about *this* one however
            # well everything else lines up. The repeated passes advance the
            # schedule, so genuine history is still walked through in order —
            # each occurrence matched against the date it was actually due.
            if best.date <= 0.0:
                continue

            if best.total >= AUTO_THRESHOLD:
                best_payment.last_matched_txn_id = txn.id
                best_payment.last_matched_date = txn_date
                _advance_next_due(best_payment, txn_date)
                used_payments.add(best_payment.id)
                claimed.add(txn.id)
                matched_this_pass += 1
            else:
                # The middle band: plausible but not certain. Recorded rather
                # than acted on, so it is visible instead of silently dropped.
                proposed += _record_proposal(db, best_payment, txn, best)

        if matched_this_pass == 0:
            break
        db.flush()
        total_matched += matched_this_pass
        passes += 1

    db.flush()
    return {"matched": total_matched, "proposed": proposed, "passes": passes}


def _record_proposal(db: "Session", payment, txn, scored: "MatchScore") -> int:
    """Record a plausible-but-uncertain match. Returns 1 if newly added."""
    import json

    from sqlalchemy import select

    from app.models.scheduled_match_proposal import (
        STATUS_PENDING, ScheduledMatchProposal,
    )

    existing = db.execute(
        select(ScheduledMatchProposal).where(
            ScheduledMatchProposal.scheduled_payment_id == payment.id,
            ScheduledMatchProposal.transaction_id == txn.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Already decided, or already waiting — never re-ask a question the
        # user has answered.
        return 0

    db.add(ScheduledMatchProposal(
        scheduled_payment_id=payment.id,
        transaction_id=txn.id,
        score=scored.total,
        components=json.dumps(scored.as_dict()),
        status=STATUS_PENDING,
    ))
    return 1


def drop_proposals_for_payment(db: "Session", payment_id: int) -> int:
    """Remove a schedule's proposals before it is deleted.

    scheduled_match_proposals.scheduled_payment_id is a foreign key with no
    cascade, so deleting a schedule that has one raises IntegrityError — the
    same shape of bug as last_matched_txn_id, and it takes out the delete
    button rather than surfacing anywhere useful.
    """
    from sqlalchemy import select

    from app.models.scheduled_match_proposal import ScheduledMatchProposal

    rows = db.execute(
        select(ScheduledMatchProposal).where(
            ScheduledMatchProposal.scheduled_payment_id == payment_id
        )
    ).scalars().all()
    for row in rows:
        db.delete(row)
    if rows:
        db.flush()
    return len(rows)


def pending_proposals(db: "Session") -> list:
    """Proposals awaiting a decision, strongest first."""
    from sqlalchemy import select

    from app.models.scheduled_match_proposal import (
        STATUS_PENDING, ScheduledMatchProposal,
    )

    return db.execute(
        select(ScheduledMatchProposal)
        .where(ScheduledMatchProposal.status == STATUS_PENDING)
        .order_by(ScheduledMatchProposal.score.desc())
    ).scalars().all()


def confirm_proposal(db: "Session", proposal_id: int) -> bool:
    """Accept a proposal: settle the schedule against that transaction."""
    from app.models.scheduled_match_proposal import (
        STATUS_CONFIRMED, STATUS_PENDING, ScheduledMatchProposal,
    )
    from app.models.scheduled_payment import ScheduledPayment
    from app.models.transaction import Transaction
    from app.services.clock import naive_utc_now

    proposal = db.get(ScheduledMatchProposal, proposal_id)
    if proposal is None or proposal.status != STATUS_PENDING:
        return False
    payment = db.get(ScheduledPayment, proposal.scheduled_payment_id)
    txn = db.get(Transaction, proposal.transaction_id)
    if payment is None or txn is None:
        return False

    txn_date = txn.date.date() if hasattr(txn.date, "date") else txn.date
    payment.last_matched_txn_id = txn.id
    payment.last_matched_date = txn_date
    if payment.next_due_date <= txn_date:
        _advance_next_due(payment, txn_date)

    proposal.status = STATUS_CONFIRMED
    proposal.resolved_at = naive_utc_now()
    db.flush()
    return True


def reject_proposal(db: "Session", proposal_id: int) -> bool:
    """Decline a proposal. It is remembered, so it is not offered again."""
    from app.models.scheduled_match_proposal import (
        STATUS_PENDING, STATUS_REJECTED, ScheduledMatchProposal,
    )
    from app.services.clock import naive_utc_now

    proposal = db.get(ScheduledMatchProposal, proposal_id)
    if proposal is None or proposal.status != STATUS_PENDING:
        return False
    proposal.status = STATUS_REJECTED
    proposal.resolved_at = naive_utc_now()
    db.flush()
    return True
