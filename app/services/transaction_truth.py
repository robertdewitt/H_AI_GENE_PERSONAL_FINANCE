"""Cascade truth-layer updates when a transaction is edited."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment_decomposition import PaymentDecomposition
from app.models.transaction import Transaction
from app.services.split_service import list_splits


def cascade_event_type_to_splits(db: Session, txn: Transaction) -> int:
    """Sync event_type onto splits that are not tied to structured document lines."""
    n = 0
    for sp in list_splits(db, txn.id):
        if sp.document_line_id is None and txn.event_type:
            sp.event_type = txn.event_type
            n += 1
    if n:
        db.flush()
    return n


def flag_decomposition_stale_on_event_change(
    db: Session, transaction_id: int,
) -> None:
    """Append a note on payment decompositions when parent semantics may drift."""
    rows = db.execute(
        select(PaymentDecomposition).where(
            PaymentDecomposition.transaction_id == transaction_id,
        )
    ).scalars().all()
    hint = "[stale] parent transaction event_type changed — review decomposition."
    for row in rows:
        if row.notes:
            if hint not in row.notes:
                row.notes = f"{row.notes}\n{hint}"
        else:
            row.notes = hint
    if rows:
        db.flush()


def apply_truth_after_transaction_update(
    db: Session,
    txn: Transaction,
    old_event_type: str | None,
) -> None:
    """Run cascades after core transaction fields are saved."""
    if txn.event_type != old_event_type:
        cascade_event_type_to_splits(db, txn)
        flag_decomposition_stale_on_event_change(db, txn.id)
