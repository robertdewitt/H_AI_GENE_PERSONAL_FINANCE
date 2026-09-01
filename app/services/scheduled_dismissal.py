"""Make a deleted scheduled payment stay deleted.

The recurring detector and the statement importer both rebuild scheduled
payments from transaction history, so removing one is otherwise pointless — it
returns on the next detect or import. Every creation path consults
:func:`is_dismissed` first.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dismissed_scheduled_payment import (
    DismissedScheduledPayment,
    normalize_description,
)


def dismiss(
    db: Session,
    account_id: int,
    description: str | None,
    user_id: int | None = None,
) -> DismissedScheduledPayment | None:
    """Record that this payment shouldn't be suggested or rebuilt again."""
    key = normalize_description(description)
    if not key or account_id is None:
        return None

    existing = db.execute(
        select(DismissedScheduledPayment).where(
            DismissedScheduledPayment.account_id == account_id,
            DismissedScheduledPayment.description_key == key,
        ).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = DismissedScheduledPayment(
        account_id=account_id,
        description_key=key,
        original_description=(description or "")[:300] or None,
        user_id=user_id,
    )
    db.add(row)
    db.flush()
    return row


def is_dismissed(db: Session, account_id: int, description: str | None) -> bool:
    key = normalize_description(description)
    if not key or account_id is None:
        return False
    return db.execute(
        select(DismissedScheduledPayment.id).where(
            DismissedScheduledPayment.account_id == account_id,
            DismissedScheduledPayment.description_key == key,
        ).limit(1)
    ).scalar_one_or_none() is not None


def dismissed_keys(db: Session) -> set[tuple[int, str]]:
    """All (account_id, description_key) pairs — for filtering a batch."""
    return {
        (r.account_id, r.description_key)
        for r in db.execute(select(DismissedScheduledPayment)).scalars().all()
    }


def list_dismissed(db: Session) -> list[DismissedScheduledPayment]:
    return db.execute(
        select(DismissedScheduledPayment)
        .order_by(DismissedScheduledPayment.dismissed_at.desc())
    ).scalars().all()


def restore(db: Session, dismissal_id: int) -> bool:
    """Undo a dismissal so the payment can be detected again."""
    row = db.get(DismissedScheduledPayment, dismissal_id)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
