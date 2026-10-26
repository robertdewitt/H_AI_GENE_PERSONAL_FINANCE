import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.models.account import Account, LIABILITY_TYPES
from app.models.transaction import Transaction
from app.models.reconciliation import ReconciliationMember
from app.models.transfer_link import TransferLink
from app.schemas.transfer import TransferCandidate
from app.services.recurring_detector import _normalize


TRANSFER_KEYWORDS = [
    "transfer", "xfer", "trf", "ach", "wire", "zelle",
    "venmo", "paypal", "internal", "payment", "pymt", "pmt",
    "autopay", "auto pay", "credit", "thank you", "online pmt",
    "direct debit", "standing order",
]

PAYMENT_KEYWORDS = [
    "payment", "pymt", "pmt", "autopay", "auto pay",
    "thank you", "online pmt", "direct debit", "standing order",
    "ach", "transfer",
]

@dataclass
class _LearnedPatterns:
    """What past confirmations and dismissals say about future candidates.

    Everything is keyed by account id, so one user's history never moves
    another user's scores. A "signature" is the description with digits and
    punctuation stripped, so reference numbers and dates don't stop a
    monthly standing order from matching last month's.
    """
    routes: Counter = field(default_factory=Counter)        # (from_acct, to_acct) → confirmed count
    pairs: set = field(default_factory=set)                 # (from_acct, from_sig, to_acct, to_sig)
    sides: set = field(default_factory=set)                 # (acct, sig) seen in a confirmed link
    rejected: set = field(default_factory=set)              # (acct, sig) dismissed, never confirmed


def _signature(description: str | None) -> str:
    return _normalize(description or "")


def _learn_from_history(db: Session) -> _LearnedPatterns:
    learned = _LearnedPatterns()
    out_t, in_t = aliased(Transaction), aliased(Transaction)
    rows = db.execute(
        select(out_t.account_id, out_t.description, in_t.account_id, in_t.description)
        .select_from(TransferLink)
        .join(out_t, out_t.id == TransferLink.from_transaction_id)
        .join(in_t, in_t.id == TransferLink.to_transaction_id)
        .where(TransferLink.confirmed_by_user.is_(True))
    ).all()
    for from_acct, from_desc, to_acct, to_desc in rows:
        from_sig, to_sig = _signature(from_desc), _signature(to_desc)
        learned.routes[(from_acct, to_acct)] += 1
        learned.pairs.add((from_acct, from_sig, to_acct, to_sig))
        learned.sides.update({(from_acct, from_sig), (to_acct, to_sig)})

    dismissed = db.execute(
        select(Transaction.account_id, Transaction.description)
        .where(Transaction.transfer_dismissed.is_(True))
    ).all()
    learned.rejected = {(a, _signature(d)) for a, d in dismissed} - learned.sides
    return learned


def _learned_adjustment(
    out_txn: Transaction, in_txn: Transaction, learned: _LearnedPatterns,
) -> tuple[float, str | None]:
    """Confidence nudge from history, plus a plain-English reason for it."""
    out_side = (out_txn.account_id, _signature(out_txn.description))
    in_side = (in_txn.account_id, _signature(in_txn.description))

    if out_side in learned.rejected or in_side in learned.rejected:
        return -0.25, "Looks like a pair you marked Not a Transfer"

    delta, note = 0.0, None
    if (*out_side, *in_side) in learned.pairs:
        delta, note = 0.25, "Matches a transfer you confirmed before"
    elif out_side in learned.sides and in_side in learned.sides:
        delta = 0.15
    elif out_side in learned.sides or in_side in learned.sides:
        delta = 0.05

    route_count = learned.routes.get((out_txn.account_id, in_txn.account_id), 0)
    if route_count:
        delta += 0.10 * min(route_count, 3) / 3
        note = note or (
            f"You've confirmed {route_count} transfer{'s' if route_count != 1 else ''} "
            "between these accounts"
        )
    return delta, note


def _description_score(desc_from: str, desc_to: str) -> float:
    """Score how likely two descriptions represent a transfer."""
    lower_from = desc_from.lower()
    lower_to = desc_to.lower()
    score = 0.0

    for kw in TRANSFER_KEYWORDS:
        if kw in lower_from:
            score += 0.15
        if kw in lower_to:
            score += 0.15

    if score > 0.5:
        score = 0.5
    return score


def detect_transfers(
    db: Session,
    date_window: int | None = None,
    amount_tolerance: Decimal | None = None,
) -> list[TransferCandidate]:
    """Find unlinked transaction pairs that look like transfers.

    Considers both transactions not yet flagged AND transactions already
    flagged as ``is_transfer=True`` that have no link yet.
    """
    window = date_window or settings.transfer_date_window_days
    tolerance = amount_tolerance or settings.transfer_amount_tolerance

    learned = _learn_from_history(db)

    # Rows already reconciled as part of a multi-part payment are settled.
    grouped = select(ReconciliationMember.transaction_id)
    outflows = db.execute(
        select(Transaction).where(
            Transaction.amount < 0,
            Transaction.transfer_link_id.is_(None),
            Transaction.transfer_dismissed.is_(False),
            Transaction.id.not_in(grouped),
        )
    ).scalars().all()

    candidates: list[TransferCandidate] = []
    seen_pairs: set[tuple[int, int]] = set()

    for out_txn in outflows:
        out_abs = abs(out_txn.amount)
        date_lo = out_txn.date - timedelta(days=window)
        date_hi = out_txn.date + timedelta(days=window)

        potential_matches = db.execute(
            select(Transaction).where(
                and_(
                    Transaction.amount > 0,
                    Transaction.account_id != out_txn.account_id,
                    Transaction.transfer_link_id.is_(None),
                    Transaction.transfer_dismissed.is_(False),
                    Transaction.id.not_in(grouped),
                    Transaction.date >= date_lo,
                    Transaction.date <= date_hi,
                    Transaction.amount >= out_abs - tolerance,
                    Transaction.amount <= out_abs + tolerance,
                )
            )
        ).scalars().all()

        for in_txn in potential_matches:
            pair_key = (min(out_txn.id, in_txn.id), max(out_txn.id, in_txn.id))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            date_diff = abs((in_txn.date - out_txn.date).days)
            date_score = max(0, 1.0 - (date_diff / (window + 1)))

            amount_diff = abs(in_txn.amount - out_abs)
            amount_score = 1.0 if amount_diff <= Decimal("0.01") else max(0, 1.0 - float(amount_diff))

            desc_score = _description_score(out_txn.description, in_txn.description)
            learned_delta, learned_note = _learned_adjustment(out_txn, in_txn, learned)

            # Boost score if either side is already flagged as a transfer
            transfer_bonus = 0.0
            if out_txn.is_transfer:
                transfer_bonus += 0.15
            if in_txn.is_transfer:
                transfer_bonus += 0.15

            confidence = max(0.0, min(
                1.0,
                date_score * 0.30
                + amount_score * 0.40
                + desc_score * 0.15
                + transfer_bonus
                + learned_delta,
            ))

            out_acct = db.get(Account, out_txn.account_id)
            in_acct = db.get(Account, in_txn.account_id)

            candidates.append(TransferCandidate(
                from_transaction_id=out_txn.id,
                to_transaction_id=in_txn.id,
                amount=out_abs,
                date=out_txn.date,
                confidence=round(confidence, 3),
                from_account_name=out_acct.name if out_acct else "Unknown",
                to_account_name=in_acct.name if in_acct else "Unknown",
                from_description=out_txn.description,
                to_description=in_txn.description,
                from_currency=(out_acct.currency if out_acct else "USD") or "USD",
                to_currency=(in_acct.currency if in_acct else "USD") or "USD",
                learned_note=learned_note,
            ))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def link_transfer(
    db: Session,
    from_transaction_id: int,
    to_transaction_id: int,
    confirmed: bool = True,
    confidence: float = 1.0,
) -> TransferLink | None:
    """Create a transfer link between two transactions."""
    from_txn = db.get(Transaction, from_transaction_id)
    to_txn = db.get(Transaction, to_transaction_id)

    if not from_txn or not to_txn:
        return None

    if from_txn.transfer_link_id or to_txn.transfer_link_id:
        return None

    link = TransferLink(
        from_transaction_id=from_transaction_id,
        to_transaction_id=to_transaction_id,
        amount=abs(from_txn.amount),
        date=from_txn.date,
        confidence=confidence,
        confirmed_by_user=confirmed,
    )
    db.add(link)
    db.flush()

    from_txn.is_transfer = True
    from_txn.transfer_link_id = link.id
    to_txn.is_transfer = True
    to_txn.transfer_link_id = link.id

    db.commit()
    db.refresh(link)
    return link


def unlink_transfer(db: Session, link_id: int) -> bool:
    """Remove a transfer link and reset the transactions."""
    link = db.get(TransferLink, link_id)
    if not link:
        return False

    from_txn = db.get(Transaction, link.from_transaction_id)
    to_txn = db.get(Transaction, link.to_transaction_id)

    if from_txn:
        from_txn.is_transfer = False
        from_txn.transfer_link_id = None
    if to_txn:
        to_txn.is_transfer = False
        to_txn.transfer_link_id = None

    db.delete(link)
    db.commit()
    return True


def list_transfer_links(db: Session) -> list[TransferLink]:
    return db.execute(
        select(TransferLink).order_by(TransferLink.date.desc())
    ).scalars().all()


def scan_and_flag_payments(db: Session, user_id: int | None = None) -> int:
    """Scan liability accounts for payment-like transactions and flag them
    as transfers.  Returns the number of newly flagged transactions.

    With ``user_id`` only that user's accounts are scanned — a page action
    must not rewrite flags on someone else's ledger."""
    stmt = select(Account).where(
        Account.account_type.in_([t.value for t in LIABILITY_TYPES])
    )
    if user_id is not None:
        stmt = stmt.where(Account.user_id == user_id)
    liability_accounts = db.execute(stmt).scalars().all()

    if not liability_accounts:
        return 0

    acct_ids = [a.id for a in liability_accounts]
    unflagged = db.execute(
        select(Transaction).where(
            Transaction.account_id.in_(acct_ids),
            Transaction.amount > 0,
            Transaction.is_transfer.is_(False),
            Transaction.transfer_link_id.is_(None),
        )
    ).scalars().all()

    count = 0
    for txn in unflagged:
        desc_lower = txn.description.lower()
        if any(kw in desc_lower for kw in PAYMENT_KEYWORDS):
            txn.is_transfer = True
            count += 1

    if count:
        db.commit()
    return count


def dismiss_transfer_pair(
    db: Session,
    from_transaction_id: int,
    to_transaction_id: int,
) -> bool:
    """Mark both transactions as not transfers so they don't appear as candidates again."""
    from_txn = db.get(Transaction, from_transaction_id)
    to_txn = db.get(Transaction, to_transaction_id)
    if not from_txn or not to_txn:
        return False
    from_txn.is_transfer = False
    from_txn.transfer_dismissed = True
    to_txn.is_transfer = False
    to_txn.transfer_dismissed = True
    db.commit()
    return True


def list_unmatched_transfers(db: Session) -> list[dict]:
    """Return transactions flagged as transfers but not yet linked."""
    txns = db.execute(
        select(Transaction).where(
            Transaction.is_transfer.is_(True),
            Transaction.transfer_link_id.is_(None),
            Transaction.id.not_in(select(ReconciliationMember.transaction_id)),
        ).order_by(Transaction.date.desc())
    ).scalars().all()

    results = []
    for txn in txns:
        acct = db.get(Account, txn.account_id)
        results.append({
            "txn": txn,
            "account": acct,
        })
    return results


def transfer_partners(
    db: Session, txns: list[Transaction], owned_ids: set[int] | list[int],
) -> dict[int, Account]:
    """For each linked transaction, the account on the other side of its
    transfer — {txn_id: Account}. Only the caller's own accounts are
    returned, so a ledger never names someone else's account."""
    ids = [t.id for t in txns]
    if not ids:
        return {}
    links = db.execute(
        select(TransferLink).where(or_(
            TransferLink.from_transaction_id.in_(ids),
            TransferLink.to_transaction_id.in_(ids),
        ))
    ).scalars().all()
    other_of: dict[int, int] = {}
    wanted = set(ids)
    for link in links:
        if link.from_transaction_id in wanted:
            other_of[link.from_transaction_id] = link.to_transaction_id
        if link.to_transaction_id in wanted:
            other_of[link.to_transaction_id] = link.from_transaction_id
    # Multi-part payments (one outflow, several loan rows) are reconciliation
    # groups: the partner is a member on another account.
    mine = db.execute(
        select(ReconciliationMember.group_id, ReconciliationMember.transaction_id)
        .where(ReconciliationMember.transaction_id.in_(ids))
    ).all()
    if mine:
        members = db.execute(
            select(ReconciliationMember.group_id, Transaction.id, Transaction.account_id)
            .join(Transaction, Transaction.id == ReconciliationMember.transaction_id)
            .where(ReconciliationMember.group_id.in_({g for g, _ in mine}))
        ).all()
        txn_acct = {tid: aid for _, tid, aid in members}
        for group_id, txn_id in mine:
            for g, other, aid in members:
                if g == group_id and aid != txn_acct[txn_id]:
                    other_of.setdefault(txn_id, other)
                    break
    if not other_of:
        return {}
    acct_of = dict(db.execute(
        select(Transaction.id, Transaction.account_id)
        .where(Transaction.id.in_(set(other_of.values())))
    ).all())
    owned = set(owned_ids)
    accounts = {
        a.id: a for a in db.execute(
            select(Account).where(Account.id.in_(set(acct_of.values()) & owned))
        ).scalars().all()
    }
    return {
        txn_id: accounts[acct_of[other]]
        for txn_id, other in other_of.items()
        if other in acct_of and acct_of[other] in accounts
    }
