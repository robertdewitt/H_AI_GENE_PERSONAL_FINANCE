"""Data quality assessment — blockers and warnings first, score second.

Consumers (agents, API) should enumerate blockers/warnings rather than
interpreting the close-readiness score alone.  The score is a convenience
derivative: 100 = no blockers or warnings; each blocker subtracts 20,
each warning subtracts 5 (clamped to 0).
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.account import Account, LIABILITY_TYPES
from app.models.enums import EconomicEventType
from app.models.payment_decomposition import PaymentDecomposition
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.services.account_service import get_account_balance_rich


@dataclass
class DataQualityCounters:
    uncategorized_count: int = 0
    unclassified_count: int = 0
    low_confidence_count: int = 0
    unresolved_reconciliation_count: int = 0
    stale_valuation_count: int = 0
    liabilities_without_decomposition: int = 0
    missing_fx_count: int = 0
    unsplit_transaction_count: int = 0
    reconciliation_fx_gap_count: int = 0


@dataclass
class DataQualityReport:
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counters: DataQualityCounters = field(default_factory=DataQualityCounters)
    close_readiness_score: float = 100.0
    as_of: datetime = field(default_factory=datetime.now)

    def compute_score(self) -> None:
        score = 100.0
        score -= len(self.blockers) * 20
        score -= len(self.warnings) * 5
        self.close_readiness_score = max(0.0, score)


def assess_quality(
    db: Session,
    as_of_date: datetime | None = None,
) -> DataQualityReport:
    """Produce a data-quality report for the entire ledger."""
    now = as_of_date or datetime.now()
    report = DataQualityReport(as_of=now)

    _check_uncategorized(db, report)
    _check_unclassified(db, report)
    _check_low_confidence(db, report)
    _check_stale_balances(db, report, now)
    _check_unreconciled_transfers(db, report)
    _check_liability_health(db, report, now)
    _check_unsplit_transactions(db, report)
    _check_liabilities_without_decomposition(db, report)
    _check_reconciliation_fx_blockers(db, report)

    report.compute_score()
    return report


def _check_uncategorized(db: Session, report: DataQualityReport) -> None:
    total = db.execute(select(func.count(Transaction.id))).scalar() or 0
    uncat = db.execute(
        select(func.count(Transaction.id))
        .where(Transaction.category_id.is_(None))
    ).scalar() or 0

    report.counters.uncategorized_count = uncat

    if total == 0:
        report.blockers.append("No transactions in ledger")
        return

    pct = uncat / total
    if pct > 0.5:
        report.blockers.append(
            f"{uncat}/{total} transactions ({pct:.0%}) uncategorized"
        )
    elif pct > 0.1:
        report.warnings.append(
            f"{uncat}/{total} transactions ({pct:.0%}) uncategorized"
        )


def _check_unclassified(db: Session, report: DataQualityReport) -> None:
    count = db.execute(
        select(func.count(Transaction.id))
        .where(
            (Transaction.event_type.is_(None))
            | (Transaction.event_type == EconomicEventType.UNCLASSIFIED.value)
        )
    ).scalar() or 0
    report.counters.unclassified_count = count
    if count > 0:
        report.warnings.append(
            f"{count} transactions lack an economic event_type"
        )


def _check_low_confidence(db: Session, report: DataQualityReport) -> None:
    count = db.execute(
        select(func.count(Transaction.id))
        .where(
            Transaction.classification_confidence.isnot(None),
            Transaction.classification_confidence < 0.5,
        )
    ).scalar() or 0
    report.counters.low_confidence_count = count
    if count > 0:
        report.warnings.append(
            f"{count} transactions have low classification confidence (<0.5)"
        )


def _check_stale_balances(
    db: Session, report: DataQualityReport, now: datetime,
) -> None:
    accounts = db.execute(select(Account)).scalars().all()
    stale_count = 0
    fx_missing = 0
    for acct in accounts:
        result = get_account_balance_rich(db, acct.id)
        if result.balance_stale:
            stale_count += 1
            report.warnings.append(
                f"Account '{acct.name}' balance is stale "
                f"(as_of {result.balance_as_of})"
            )
        if result.fx.fx_stale:
            fx_missing += 1
            report.warnings.append(
                f"Account '{acct.name}' uses stale FX rate"
            )
    report.counters.stale_valuation_count = stale_count
    report.counters.missing_fx_count = fx_missing


def _check_unreconciled_transfers(
    db: Session, report: DataQualityReport,
) -> None:
    unmatched = db.execute(
        select(func.count(Transaction.id))
        .where(
            Transaction.is_transfer.is_(True),
            Transaction.transfer_link_id.is_(None),
        )
    ).scalar() or 0
    report.counters.unresolved_reconciliation_count = unmatched
    if unmatched > 10:
        report.blockers.append(
            f"{unmatched} transfers flagged but not reconciled"
        )
    elif unmatched > 0:
        report.warnings.append(
            f"{unmatched} transfers flagged but not reconciled"
        )


def _check_liability_health(
    db: Session, report: DataQualityReport, now: datetime,
) -> None:
    liabilities = db.execute(
        select(Account).where(
            Account.account_type.in_([t.value for t in LIABILITY_TYPES])
        )
    ).scalars().all()

    for acct in liabilities:
        if acct.statement_balance is None and acct.current_value is None:
            report.warnings.append(
                f"Liability '{acct.name}' has no statement or current balance"
            )
        if acct.statement_balance_as_of:
            age = (now - acct.statement_balance_as_of).days
            if age > 60:
                report.warnings.append(
                    f"Liability '{acct.name}' statement balance is {age} days old"
                )


def _check_unsplit_transactions(
    db: Session, report: DataQualityReport,
) -> None:
    """Count transactions that have no splits at all."""
    split_txn_ids = select(TransactionSplit.transaction_id).distinct()
    count = db.execute(
        select(func.count(Transaction.id))
        .where(~Transaction.id.in_(split_txn_ids))
    ).scalar() or 0
    report.counters.unsplit_transaction_count = count


def _check_liabilities_without_decomposition(
    db: Session, report: DataQualityReport,
) -> None:
    """Liability payment transactions that lack a PaymentDecomposition."""
    liability_events = [
        EconomicEventType.LIABILITY_PAYMENT.value,
        EconomicEventType.MORTGAGE_PAYMENT.value,
    ]
    decomp_txn_ids = select(PaymentDecomposition.transaction_id).distinct()

    count = db.execute(
        select(func.count(Transaction.id))
        .where(
            Transaction.event_type.in_(liability_events),
            ~Transaction.id.in_(decomp_txn_ids),
        )
    ).scalar() or 0
    report.counters.liabilities_without_decomposition = count
    if count > 0:
        report.warnings.append(
            f"{count} liability payments lack component decomposition"
        )


def _check_reconciliation_fx_blockers(
    db: Session, report: DataQualityReport,
) -> None:
    """Multi-currency reconciliation groups must have FX/base coverage."""
    from app.models.reconciliation import ReconciliationGroup
    from app.services.reconciliation_invariants import validate_group

    groups = db.execute(select(ReconciliationGroup)).scalars().all()
    gap = 0
    for g in groups:
        members = g.members
        if len({m.allocated_currency for m in members}) <= 1:
            continue
        res = validate_group(db, g)
        if res.members_missing_base > 0 or res.fx_stale_members:
            gap += 1
            report.blockers.append(
                f"Reconciliation group {g.id}: multi-currency legs need "
                f"allocated_amount_base or valid FX (net unresolved={res.net_base})"
            )
    report.counters.reconciliation_fx_gap_count = gap
