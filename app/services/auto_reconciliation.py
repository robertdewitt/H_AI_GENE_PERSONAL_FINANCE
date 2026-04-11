"""Create reconciliation groups for obvious transfer pairs."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.account import Account
from app.models.enums import (
    ClassificationProvenance,
    FxTreatmentMode,
    ReconciliationGroupType,
    ReconciliationStatus,
)
from app.models.reconciliation import ReconciliationGroup, ReconciliationMember
from app.models.transaction import Transaction


def _txn_in_any_group(db: Session, txn_id: int) -> bool:
    q = select(func.count(ReconciliationMember.id)).where(
        ReconciliationMember.transaction_id == txn_id,
    )
    return (db.execute(q).scalar() or 0) > 0


def create_suggested_transfer_groups(
    db: Session,
    limit: int = 200,
    amount_tolerance: float = 0.02,
) -> int:
    """Pair unmatched transfer rows into ReconciliationGroup (suggested).

    Matches: opposite signed amounts, different accounts, within date window,
    neither leg already in a reconciliation group.
    """
    window = timedelta(days=settings.transfer_date_window_days)
    base_ccy = settings.base_currency

    candidates = db.execute(
        select(Transaction)
        .where(
            Transaction.is_transfer.is_(True),
            Transaction.transfer_link_id.is_(None),
        )
        .order_by(Transaction.date.desc())
        .limit(limit * 2)
    ).scalars().all()

    used: set[int] = set()
    created = 0

    for t1 in candidates:
        if t1.id in used or _txn_in_any_group(db, t1.id):
            continue
        lo = t1.date - window
        hi = t1.date + window
        partner = db.execute(
            select(Transaction)
            .join(Account, Transaction.account_id == Account.id)
            .where(
                Transaction.id != t1.id,
                Transaction.account_id != t1.account_id,
                Transaction.date >= lo,
                Transaction.date <= hi,
                Transaction.is_transfer.is_(True),
                Transaction.transfer_link_id.is_(None),
                func.abs(Transaction.amount + t1.amount) < amount_tolerance,
            )
            .order_by(func.abs(Transaction.date - t1.date))
            .limit(1)
        ).scalar_one_or_none()

        if not partner or partner.id in used or _txn_in_any_group(db, partner.id):
            continue

        a1 = db.get(Account, t1.account_id)
        a2 = db.get(Account, partner.account_id)
        if not a1 or not a2:
            continue

        grp = ReconciliationGroup(
            group_type=ReconciliationGroupType.TRANSFER.value,
            status=ReconciliationStatus.SUGGESTED.value,
            base_currency=base_ccy,
            fx_treatment=FxTreatmentMode.NONE.value,
            tolerance_base=amount_tolerance,
            provenance=ClassificationProvenance.INFERRED.value,
            as_of_date=max(t1.date, partner.date),
            notes="auto-matched transfer pair",
        )
        db.add(grp)
        db.flush()

        db.add_all([
            ReconciliationMember(
                group_id=grp.id,
                transaction_id=t1.id,
                allocated_amount_native=t1.amount,
                allocated_currency=a1.currency,
                allocated_amount_base=t1.amount if a1.currency == base_ccy else None,
                role="source" if t1.amount < 0 else "destination",
            ),
            ReconciliationMember(
                group_id=grp.id,
                transaction_id=partner.id,
                allocated_amount_native=partner.amount,
                allocated_currency=a2.currency,
                allocated_amount_base=partner.amount if a2.currency == base_ccy else None,
                role="source" if partner.amount < 0 else "destination",
            ),
        ])
        used.add(t1.id)
        used.add(partner.id)
        created += 1
        if created >= limit:
            break

    if created:
        db.flush()
    return created
