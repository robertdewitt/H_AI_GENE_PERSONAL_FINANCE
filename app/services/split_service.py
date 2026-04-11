"""CRUD and validation for TransactionSplit allocations.

Design principle: users split transactions by *category* only.
Economic event type, spend_type, and counts_as_true_spend are all
system-derived from the parent transaction's event_type via
event_type_to_spend_metadata().  Users never set these directly.

Core invariant: sum of split amounts must equal the parent transaction
amount (within tolerance), unless explicitly marked unresolved.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.enums import ClassificationProvenance, SpendType
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.services.event_classifier import event_type_to_spend_metadata


@dataclass
class SplitValidation:
    valid: bool = False
    split_sum: float = 0.0
    transaction_amount: float = 0.0
    residual: float = 0.0
    tolerance: float = 0.01
    split_count: int = 0
    warnings: list[str] = field(default_factory=list)


def list_splits(db: Session, transaction_id: int) -> list[TransactionSplit]:
    return db.execute(
        select(TransactionSplit)
        .where(TransactionSplit.transaction_id == transaction_id)
        .order_by(TransactionSplit.id)
    ).scalars().all()


def add_split(
    db: Session,
    transaction_id: int,
    amount_native: float,
    currency: str,
    category_id: int | None = None,
    # Spend metadata — auto-derived from parent event_type when omitted.
    # Pass explicitly only for document-initiated splits (payroll, rental).
    spend_type: SpendType | str | None = None,
    counts_as_true_spend: bool | None = None,
    # Internal linkage fields — not user-facing
    linked_account_id: int | None = None,
    linked_reconciliation_group_id: int | None = None,
    document_line_id: int | None = None,
    amount_base: float | None = None,
    fx_rate: float | None = None,
    provenance: str = ClassificationProvenance.IMPORTED.value,
    confidence: float | None = None,
    notes: str | None = None,
    as_of_date: datetime | None = None,
    # Allow caller to override event_type on the split (document path)
    event_type: str | None = None,
) -> TransactionSplit:
    """Create a split.  spend_type and counts_as_true_spend are auto-derived
    from the parent transaction's event_type unless explicitly provided.
    """
    # Resolve event_type: prefer explicit override, otherwise load from parent.
    resolved_event_type = event_type
    if resolved_event_type is None:
        txn = db.get(Transaction, transaction_id)
        if txn and txn.event_type:
            resolved_event_type = txn.event_type

    # Auto-derive spend metadata when not explicitly provided.
    if spend_type is None and counts_as_true_spend is None:
        derived_st, derived_cats = event_type_to_spend_metadata(resolved_event_type)
        st_val = derived_st
        cats_val = derived_cats
    else:
        st_val = spend_type.value if isinstance(spend_type, SpendType) else spend_type
        cats_val = counts_as_true_spend if counts_as_true_spend is not None else False

    split = TransactionSplit(
        transaction_id=transaction_id,
        amount_native=amount_native,
        currency=currency,
        amount_base=amount_base,
        fx_rate=fx_rate,
        event_type=resolved_event_type,
        category_id=category_id,
        linked_account_id=linked_account_id,
        linked_reconciliation_group_id=linked_reconciliation_group_id,
        document_line_id=document_line_id,
        counts_as_true_spend=cats_val,
        spend_type=st_val,
        provenance=provenance,
        confidence=confidence,
        as_of_date=as_of_date,
        notes=notes,
    )
    db.add(split)
    db.flush()
    return split


def validate_splits(
    db: Session, transaction_id: int, tolerance: float = 0.01,
) -> SplitValidation:
    txn = db.get(Transaction, transaction_id)
    if not txn:
        return SplitValidation(warnings=["Transaction not found"])

    splits = list_splits(db, transaction_id)
    split_sum = sum(s.amount_native for s in splits)
    residual = txn.amount - split_sum
    valid = abs(residual) <= tolerance

    warnings: list[str] = []
    if not splits:
        warnings.append("No splits exist for this transaction")
    if not valid:
        warnings.append(
            f"Split sum {split_sum:.2f} != transaction amount "
            f"{txn.amount:.2f} (residual {residual:.2f})"
        )

    return SplitValidation(
        valid=valid,
        split_sum=split_sum,
        transaction_amount=txn.amount,
        residual=residual,
        tolerance=tolerance,
        split_count=len(splits),
        warnings=warnings,
    )


def create_default_split(db: Session, transaction_id: int) -> TransactionSplit:
    """Create a single pass-through split that mirrors the parent transaction.

    Category and spend metadata are derived from the parent transaction.
    """
    txn = db.get(Transaction, transaction_id)
    if not txn:
        raise ValueError(f"Transaction {transaction_id} not found")

    return add_split(
        db,
        transaction_id=txn.id,
        amount_native=txn.amount,
        currency=txn.original_currency,
        amount_base=txn.amount_base,
        fx_rate=txn.exchange_rate,
        category_id=txn.category_id,
        provenance=ClassificationProvenance.INFERRED.value,
        confidence=txn.classification_confidence,
        # event_type/spend_type/counts_as_true_spend auto-derived via add_split
    )


def delete_splits_for_transaction(db: Session, transaction_id: int) -> int:
    """Remove all splits for a transaction (except document-linked lines).
    Returns rows deleted.
    """
    r = db.execute(
        delete(TransactionSplit).where(
            TransactionSplit.transaction_id == transaction_id,
            TransactionSplit.document_line_id.is_(None),
        )
    )
    db.flush()
    return r.rowcount or 0


def replace_transaction_splits(
    db: Session,
    transaction_id: int,
    lines: list[dict],
    *,
    default_provenance: str = ClassificationProvenance.USER_CONFIRMED.value,
) -> SplitValidation:
    """Replace manual splits from a list of category+amount dicts.

    Each dict should contain:
      - amount       (required)
      - category_id  (optional int)
      - notes        (optional str)
      - currency     (optional, defaults to parent transaction currency)

    spend_type and counts_as_true_spend are auto-derived from the parent
    transaction's event_type — users do not set these directly.

    Document-linked splits (document_line_id is not None) are preserved
    and never replaced by this function.
    """
    txn = db.get(Transaction, transaction_id)
    if not txn:
        return SplitValidation(warnings=["Transaction not found"])

    # Preserve document-linked splits; only remove manual ones.
    delete_splits_for_transaction(db, transaction_id)

    ccy = txn.original_currency or "USD"
    for row in lines:
        amt = float(row["amount"])
        line_ccy = str(row.get("currency") or ccy)
        cat_id = row.get("category_id")
        if cat_id is not None:
            cat_id = int(cat_id)

        add_split(
            db,
            transaction_id=transaction_id,
            amount_native=amt,
            currency=line_ccy,
            category_id=cat_id,
            provenance=str(row.get("provenance", default_provenance)),
            confidence=row.get("confidence"),
            notes=row.get("notes"),
            as_of_date=row.get("as_of_date") or txn.date,
            # spend_type and counts_as_true_spend auto-derived from parent event_type
        )

    db.flush()
    return validate_splits(db, transaction_id)


def get_true_spend(
    db: Session, account_id: int | None = None,
) -> float:
    """Sum all splits where counts_as_true_spend is True."""
    q = select(func.coalesce(func.sum(TransactionSplit.amount_native), 0.0)).where(
        TransactionSplit.counts_as_true_spend.is_(True),
    )
    if account_id:
        q = q.join(Transaction).where(Transaction.account_id == account_id)
    return float(db.execute(q).scalar() or 0.0)
