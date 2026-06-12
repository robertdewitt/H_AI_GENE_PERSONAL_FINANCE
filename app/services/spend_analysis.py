"""Category time series and spend analysis — derived from splits ONLY.

Raw transaction amounts are NOT used for spend analysis. Only splits
with counts_as_true_spend=True or explicit spend_type contribute.
This enforces the principle that raw rows are not truth.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from app.services.clock import naive_utc_now
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models.category import Category
from app.models.enums import SpendType
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit


@dataclass
class MonthlySpendRow:
    month: str
    spend_type: str | None
    category: str | None
    total: Decimal
    count: int


@dataclass
class SpendSummary:
    period_months: int
    total_true_spend: Decimal = Decimal("0.00")
    by_spend_type: dict[str, Decimal] = field(default_factory=dict)
    by_category: list[dict] = field(default_factory=list)
    monthly: list[MonthlySpendRow] = field(default_factory=list)


def compute_spend_summary(
    db: Session,
    months: int = 3,
    account_id: int | None = None,
) -> SpendSummary:
    since = naive_utc_now() - timedelta(days=months * 30)

    base_filter = and_(
        TransactionSplit.counts_as_true_spend.is_(True),
        Transaction.date >= since,
    )
    base_join = select(TransactionSplit).join(Transaction)
    if account_id:
        base_filter = and_(base_filter, Transaction.account_id == account_id)

    # Total true spend
    total = db.execute(
        select(func.coalesce(func.sum(TransactionSplit.amount_native), 0))
        .select_from(TransactionSplit)
        .join(Transaction)
        .where(base_filter)
    ).scalar() or Decimal("0.00")

    # By spend type
    type_rows = db.execute(
        select(
            TransactionSplit.spend_type,
            func.sum(TransactionSplit.amount_native).label("total"),
        )
        .join(Transaction)
        .where(base_filter)
        .group_by(TransactionSplit.spend_type)
    ).all()

    by_type = {}
    for r in type_rows:
        key = r.spend_type or "unclassified"
        by_type[key] = round(r.total or Decimal("0.00"), 2)

    # By category (from split.category_id)
    cat_rows = db.execute(
        select(
            Category.name,
            func.sum(TransactionSplit.amount_native).label("total"),
            func.count(TransactionSplit.id).label("count"),
        )
        .select_from(TransactionSplit)
        .join(Transaction)
        .join(Category, TransactionSplit.category_id == Category.id)
        .where(base_filter)
        .group_by(Category.id)
        .order_by(func.sum(TransactionSplit.amount_native))
    ).all()

    by_category = [
        {"category": r.name, "total": round(r.total or Decimal("0.00"), 2), "count": r.count}
        for r in cat_rows
    ]

    # Monthly breakdown
    monthly_rows = db.execute(
        select(
            func.strftime("%Y-%m", Transaction.date).label("month"),
            TransactionSplit.spend_type,
            func.sum(TransactionSplit.amount_native).label("total"),
            func.count(TransactionSplit.id).label("count"),
        )
        .select_from(TransactionSplit)
        .join(Transaction)
        .where(base_filter)
        .group_by("month", TransactionSplit.spend_type)
        .order_by("month")
    ).all()

    monthly = [
        MonthlySpendRow(
            month=r.month,
            spend_type=r.spend_type,
            category=None,
            total=round(r.total or Decimal("0.00"), 2),
            count=r.count,
        )
        for r in monthly_rows
    ]

    return SpendSummary(
        period_months=months,
        total_true_spend=round(total, 2),
        by_spend_type=by_type,
        by_category=by_category,
        monthly=monthly,
    )
