"""CRUD and validation for PaymentDecomposition rows.

Invariant: component amounts for a transaction should sum to the
transaction's amount (within tolerance).
"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ClassificationProvenance, PaymentComponent
from app.models.payment_decomposition import PaymentDecomposition
from app.models.transaction import Transaction


@dataclass
class DecompositionValidation:
    valid: bool = False
    component_sum: float = 0.0
    transaction_amount: float = 0.0
    residual: float = 0.0
    tolerance: float = 0.01
    warnings: list[str] = field(default_factory=list)


def list_for_transaction(
    db: Session, transaction_id: int,
) -> list[PaymentDecomposition]:
    return db.execute(
        select(PaymentDecomposition)
        .where(PaymentDecomposition.transaction_id == transaction_id)
        .order_by(PaymentDecomposition.id)
    ).scalars().all()


def add_component(
    db: Session,
    transaction_id: int,
    component: PaymentComponent,
    amount: float,
    currency: str,
    amount_base: float | None = None,
    provenance: ClassificationProvenance = ClassificationProvenance.IMPORTED,
    confidence: float | None = None,
    notes: str | None = None,
) -> PaymentDecomposition:
    row = PaymentDecomposition(
        transaction_id=transaction_id,
        component=component.value,
        amount=amount,
        currency=currency,
        amount_base=amount_base,
        provenance=provenance.value,
        confidence=confidence,
        notes=notes,
    )
    db.add(row)
    db.flush()
    return row


def validate_decomposition(
    db: Session, transaction_id: int, tolerance: float = 0.01,
) -> DecompositionValidation:
    """Check that component amounts sum to the transaction amount."""
    txn = db.get(Transaction, transaction_id)
    if not txn:
        return DecompositionValidation(
            warnings=["Transaction not found"],
        )

    rows = list_for_transaction(db, transaction_id)
    comp_sum = sum(r.amount for r in rows)
    residual = abs(txn.amount) - abs(comp_sum)
    valid = abs(residual) <= tolerance

    warnings = []
    if not rows:
        warnings.append("No decomposition rows exist")
    if not valid:
        warnings.append(
            f"Component sum {comp_sum} differs from transaction "
            f"amount {txn.amount} by {residual}"
        )

    return DecompositionValidation(
        valid=valid,
        component_sum=comp_sum,
        transaction_amount=txn.amount,
        residual=residual,
        tolerance=tolerance,
        warnings=warnings,
    )
