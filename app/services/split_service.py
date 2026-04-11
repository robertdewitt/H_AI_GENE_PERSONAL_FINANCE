"""CRUD and validation for TransactionSplit allocations.

Core invariant: sum of split amounts must equal the parent transaction
amount (within tolerance), unless explicitly marked unresolved.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.enums import (
    ClassificationProvenance,
    EconomicEventType,
    SpendType,
)
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit


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
    event_type: EconomicEventType | str | None = None,
    spend_type: SpendType | str | None = None,
    counts_as_true_spend: bool = False,
    category_id: int | None = None,
    linked_account_id: int | None = None,
    linked_reconciliation_group_id: int | None = None,
    document_line_id: int | None = None,
    amount_base: float | None = None,
    fx_rate: float | None = None,
    provenance: str = ClassificationProvenance.IMPORTED.value,
    confidence: float | None = None,
    notes: str | None = None,
    as_of_date: datetime | None = None,
) -> TransactionSplit:
    et = event_type.value if isinstance(event_type, EconomicEventType) else event_type
    st = spend_type.value if isinstance(spend_type, SpendType) else spend_type

    split = TransactionSplit(
        transaction_id=transaction_id,
        amount_native=amount_native,
        currency=currency,
        amount_base=amount_base,
        fx_rate=fx_rate,
        event_type=et,
        category_id=category_id,
        linked_account_id=linked_account_id,
        linked_reconciliation_group_id=linked_reconciliation_group_id,
        document_line_id=document_line_id,
        counts_as_true_spend=counts_as_true_spend,
        spend_type=st,
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
    """Create a single pass-through split that mirrors the parent transaction."""
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
        event_type=txn.event_type,
        provenance=ClassificationProvenance.INFERRED.value,
        confidence=txn.classification_confidence,
    )


def delete_splits_for_transaction(db: Session, transaction_id: int) -> int:
    """Remove all splits for a transaction. Returns rows deleted."""
    r = db.execute(
        delete(TransactionSplit).where(
            TransactionSplit.transaction_id == transaction_id,
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
    """Replace splits from a list of dicts: amount, currency (optional),
    spend_type, event_type, category_id, notes, counts_as_true_spend (optional).

    Sum of amounts must match the parent transaction amount within tolerance.
    """
    txn = db.get(Transaction, transaction_id)
    if not txn:
        return SplitValidation(warnings=["Transaction not found"])

    delete_splits_for_transaction(db, transaction_id)

    ccy = txn.original_currency or "USD"
    for row in lines:
        amt = float(row["amount"])
        line_ccy = str(row.get("currency") or ccy)
        st_raw = row.get("spend_type")
        st = None
        if st_raw:
            try:
                st = SpendType(str(st_raw))
            except ValueError:
                st = None
        ev_raw = row.get("event_type")
        ev = None
        if ev_raw:
            try:
                ev = EconomicEventType(str(ev_raw))
            except ValueError:
                ev = None
        cat_id = row.get("category_id")
        if cat_id is not None:
            cat_id = int(cat_id)
        counts = bool(row.get("counts_as_true_spend", st is not None))
        add_split(
            db,
            transaction_id=transaction_id,
            amount_native=amt,
            currency=line_ccy,
            event_type=ev,
            spend_type=st,
            counts_as_true_spend=counts,
            category_id=cat_id,
            provenance=str(row.get("provenance", default_provenance)),
            confidence=row.get("confidence"),
            notes=row.get("notes"),
            as_of_date=row.get("as_of_date") or txn.date,
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
