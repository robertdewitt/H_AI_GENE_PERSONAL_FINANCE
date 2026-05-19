from dataclasses import dataclass, field
from difflib import SequenceMatcher
from decimal import Decimal

from sqlalchemy import func, select, or_
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.transaction import Transaction


@dataclass
class DuplicateGroup:
    account_id: int
    account_name: str
    date: str
    amount: Decimal
    transactions: list           # list of Transaction
    confidence: float            # 0–1; 1.0 = identical descriptions
    currency: str = "USD"        # account currency
    cross_batch: bool = False    # True when txns came from different import files
    ollama_score: float | None = None   # LLM duplicate probability (0–1)
    ollama_suggested: bool = False      # True when LLM says likely duplicate


def _description_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _dismissed_keys(db: Session) -> set[tuple]:
    from app.models.dismissed_duplicate import DismissedDuplicate
    rows = db.execute(select(DismissedDuplicate)).scalars().all()
    return {(r.account_id, r.txn_date, r.amount) for r in rows}


def find_dismissed_groups(db: Session) -> list[DuplicateGroup]:
    """Same as find_duplicate_groups but returns only dismissed groups."""
    from app.models.dismissed_duplicate import DismissedDuplicate
    dismissed_rows = db.execute(select(DismissedDuplicate)).scalars().all()
    if not dismissed_rows:
        return []

    from sqlalchemy import func as _func
    clauses = [
        (
            (Transaction.account_id == r.account_id)
            & (_func.date(Transaction.date) == r.txn_date)
            & (Transaction.amount == r.amount)
        )
        for r in dismissed_rows
    ]
    txns = db.execute(
        select(Transaction)
        .where(or_(*clauses))
        .order_by(Transaction.account_id, Transaction.date, Transaction.amount, Transaction.id)
    ).scalars().all()

    acct_ids = {t.account_id for t in txns}
    accounts = {a.id: a for a in db.execute(
        select(Account).where(Account.id.in_(acct_ids))
    ).scalars().all()} if acct_ids else {}

    # Only return groups that still have 2+ transactions (not yet cleaned up)
    buckets: dict[tuple, list[Transaction]] = {}
    for txn in txns:
        date_key = txn.date.date() if hasattr(txn.date, "date") else txn.date
        key = (txn.account_id, str(date_key), txn.amount)
        buckets.setdefault(key, []).append(txn)

    result: list[DuplicateGroup] = []
    for (acct_id, date_str, amount), txn_list in buckets.items():
        if len(txn_list) < 2:
            continue
        descs = [t.description or "" for t in txn_list]
        pairs = [
            _description_similarity(descs[i], descs[j])
            for i in range(len(descs))
            for j in range(i + 1, len(descs))
        ]
        confidence = min(pairs) if pairs else 1.0
        batch_ids = {t.import_batch_id for t in txn_list}
        acct = accounts.get(acct_id)
        result.append(DuplicateGroup(
            account_id=acct_id,
            account_name=acct.name if acct else f"Account {acct_id}",
            date=date_str,
            amount=amount,
            transactions=txn_list,
            confidence=round(confidence, 3),
            currency=acct.currency if acct else "USD",
            cross_batch=len(batch_ids) > 1,
        ))

    result.sort(key=lambda g: (not g.cross_batch, -g.confidence))
    return result


def find_duplicate_groups(db: Session) -> list[DuplicateGroup]:
    """Return groups of 2+ transactions sharing (account_id, date, amount).

    Groups where transactions came from different import batches are flagged
    with cross_batch=True and sorted to the top — these are the most likely
    real duplicates (same transaction imported from two different files).

    Within each group, confidence is the minimum pairwise description
    similarity across all pairs.
    """
    # (account_id, date, amount) combos that appear more than once
    dupe_keys = db.execute(
        select(
            Transaction.account_id,
            Transaction.date,
            Transaction.amount,
        )
        .group_by(Transaction.account_id, Transaction.date, Transaction.amount)
        .having(func.count() > 1)
    ).all()

    if not dupe_keys:
        return []

    dismissed = _dismissed_keys(db)

    clauses = [
        (
            (Transaction.account_id == row.account_id)
            & (Transaction.date == row.date)
            & (Transaction.amount == row.amount)
        )
        for row in dupe_keys
    ]
    txns = db.execute(
        select(Transaction)
        .where(or_(*clauses))
        .order_by(Transaction.account_id, Transaction.date, Transaction.amount, Transaction.id)
    ).scalars().all()

    # Bucket by (account_id, date, amount)
    buckets: dict[tuple, list[Transaction]] = {}
    for txn in txns:
        date_key = txn.date.date() if hasattr(txn.date, "date") else txn.date
        key = (txn.account_id, date_key, txn.amount)
        buckets.setdefault(key, []).append(txn)

    # Load accounts once
    acct_ids = {k[0] for k in buckets}
    accounts = {a.id: a for a in db.execute(
        select(Account).where(Account.id.in_(acct_ids))
    ).scalars().all()}

    result: list[DuplicateGroup] = []
    for (acct_id, date, amount), txn_list in buckets.items():
        if (acct_id, str(date), amount) in dismissed:
            continue
        descs = [t.description or "" for t in txn_list]
        pairs = [
            _description_similarity(descs[i], descs[j])
            for i in range(len(descs))
            for j in range(i + 1, len(descs))
        ]
        confidence = min(pairs) if pairs else 1.0

        batch_ids = {t.import_batch_id for t in txn_list}
        cross_batch = len(batch_ids) > 1

        # Skip same-batch groups where descriptions are clearly different —
        # these are coincidental same-date/amount transactions, not duplicates.
        # Cross-batch groups always surface (different files = real duplicate).
        if not cross_batch and confidence < 0.5:
            continue

        acct = accounts.get(acct_id)
        result.append(DuplicateGroup(
            account_id=acct_id,
            account_name=acct.name if acct else f"Account {acct_id}",
            date=str(date),
            amount=amount,
            transactions=txn_list,
            confidence=round(confidence, 3),
            currency=acct.currency if acct else "USD",
            cross_batch=cross_batch,
        ))

    # Sort: cross-batch first (most likely real duplicates), then by confidence desc
    result.sort(key=lambda g: (not g.cross_batch, -g.confidence))
    return result
