"""Auto-generate splits when missing — pass-through or category mirror."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.services.split_service import create_default_split, list_splits


def ensure_default_split_if_missing(db: Session, transaction_id: int) -> bool:
    """If the transaction has no splits, add a single pass-through split."""
    n = db.execute(
        select(func.count(TransactionSplit.id)).where(
            TransactionSplit.transaction_id == transaction_id,
        )
    ).scalar() or 0
    if n > 0:
        return False
    create_default_split(db, transaction_id)
    db.flush()
    return True


def ensure_splits_after_import(db: Session, transaction_ids: list[int]) -> int:
    """Called after import + classification — one pass-through split per txn."""
    created = 0
    for tid in transaction_ids:
        if ensure_default_split_if_missing(db, tid):
            created += 1
    return created


def ensure_split_for_categorized_transaction(db: Session, txn: Transaction) -> None:
    """If txn has category but no splits, mirror amount into one split with category."""
    if not txn.category_id:
        return
    if list_splits(db, txn.id):
        return
    from app.services.split_service import add_split
    from app.models.enums import ClassificationProvenance

    add_split(
        db,
        transaction_id=txn.id,
        amount_native=txn.amount,
        currency=txn.original_currency or "USD",
        category_id=txn.category_id,
        provenance=ClassificationProvenance.RULE_DERIVED.value,
        notes="auto: category mirror",
    )
    db.flush()
