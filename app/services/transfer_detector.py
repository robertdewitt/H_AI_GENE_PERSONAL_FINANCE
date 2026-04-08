from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.account import Account, LIABILITY_TYPES
from app.models.transaction import Transaction
from app.models.transfer_link import TransferLink
from app.schemas.transfer import TransferCandidate


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
    amount_tolerance: float | None = None,
) -> list[TransferCandidate]:
    """Find unlinked transaction pairs that look like transfers.

    Considers both transactions not yet flagged AND transactions already
    flagged as ``is_transfer=True`` that have no link yet.
    """
    window = date_window or settings.transfer_date_window_days
    tolerance = amount_tolerance or settings.transfer_amount_tolerance

    outflows = db.execute(
        select(Transaction).where(
            Transaction.amount < 0,
            Transaction.transfer_link_id.is_(None),
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
            amount_score = 1.0 if amount_diff <= 0.01 else max(0, 1.0 - amount_diff)

            desc_score = _description_score(
                out_txn.description, in_txn.description
            )

            # Boost score if either side is already flagged as a transfer
            transfer_bonus = 0.0
            if out_txn.is_transfer:
                transfer_bonus += 0.15
            if in_txn.is_transfer:
                transfer_bonus += 0.15

            confidence = min(
                1.0,
                date_score * 0.30
                + amount_score * 0.40
                + desc_score * 0.15
                + transfer_bonus,
            )

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


def scan_and_flag_payments(db: Session) -> int:
    """Scan liability accounts for payment-like transactions and flag them
    as transfers.  Returns the number of newly flagged transactions."""
    liability_accounts = db.execute(
        select(Account).where(
            Account.account_type.in_([t.value for t in LIABILITY_TYPES])
        )
    ).scalars().all()

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


def list_unmatched_transfers(db: Session) -> list[dict]:
    """Return transactions flagged as transfers but not yet linked."""
    txns = db.execute(
        select(Transaction).where(
            Transaction.is_transfer.is_(True),
            Transaction.transfer_link_id.is_(None),
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
