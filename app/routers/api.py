"""JSON API for LLM agents and programmatic access.

All endpoints return structured JSON designed for consumption by LLM agents
that analyze spending habits, investments, and financial health.
"""
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.account import Account, AccountType, ASSET_TYPES, LIABILITY_TYPES
from app.models.category import Category
from app.models.enums import EconomicEventType
from app.models.transaction import Transaction
from app.services.account_service import (
    get_account_balance,
    get_account_balance_rich,
    list_accounts,
)
from app.services.data_quality import assess_quality
from app.services.net_worth_service import compute_net_worth, compute_net_worth_series

router = APIRouter(prefix="/api/v1", tags=["api"])


# ── Accounts ─────────────────────────────────────────────────────────


@router.get("/accounts")
def api_accounts(db: Session = Depends(get_db)):
    """All accounts with current balances in both native and base currency."""
    accounts = list_accounts(db)
    base = settings.base_currency
    result = []
    for acct in accounts:
        native_bal = get_account_balance(
            db, acct.id, target_currency=acct.currency,
        )
        base_bal = get_account_balance(
            db, acct.id, target_currency=base,
        )
        txn_count = db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.account_id == acct.id
            )
        ).scalar() or 0

        result.append({
            "id": acct.id,
            "name": acct.name,
            "type": acct.account_type.value,
            "type_group": acct.type_group,
            "institution": acct.institution,
            "currency": acct.currency,
            "is_asset": acct.is_asset,
            "balance_native": round(native_bal, 2),
            "balance_base": round(base_bal, 2),
            "base_currency": base,
            "transaction_count": txn_count,
        })
    return {"accounts": result, "base_currency": base}


# ── Transactions ─────────────────────────────────────────────────────


@router.get("/transactions")
def api_transactions(
    account_id: int | None = Query(None),
    category_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    search: str | None = Query(None),
    is_transfer: bool | None = Query(None),
    uncategorized: bool | None = Query(None),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Filtered transactions as structured JSON."""
    q = select(Transaction)
    if account_id:
        q = q.where(Transaction.account_id == account_id)
    if category_id:
        q = q.where(Transaction.category_id == category_id)
    if uncategorized:
        q = q.where(Transaction.category_id.is_(None))
    if date_from:
        try:
            q = q.where(
                Transaction.date >= datetime.strptime(date_from, "%Y-%m-%d")
            )
        except ValueError:
            pass
    if date_to:
        try:
            q = q.where(
                Transaction.date <= datetime.strptime(date_to, "%Y-%m-%d")
            )
        except ValueError:
            pass
    if search:
        q = q.where(Transaction.description.ilike(f"%{search}%"))
    if is_transfer is not None:
        q = q.where(Transaction.is_transfer == is_transfer)

    total = db.execute(
        select(func.count()).select_from(q.subquery())
    ).scalar() or 0

    txns = db.execute(
        q.order_by(Transaction.date.desc()).offset(offset).limit(limit)
    ).scalars().all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "transactions": [
            {
                "id": t.id,
                "date": t.date.strftime("%Y-%m-%d"),
                "description": t.description,
                "amount": round(t.amount, 2),
                "currency": t.original_currency,
                "account_id": t.account_id,
                "account_name": t.account.name if t.account else None,
                "category": t.category.name if t.category else None,
                "category_id": t.category_id,
                "is_transfer": t.is_transfer,
                "event_type": t.event_type,
                "classification_provenance": t.classification_provenance,
                "classification_confidence": t.classification_confidence,
            }
            for t in txns
        ],
    }


# ── Categories ───────────────────────────────────────────────────────


@router.get("/categories")
def api_categories(db: Session = Depends(get_db)):
    """All categories with transaction counts and totals."""
    cats = db.execute(
        select(Category).order_by(Category.name)
    ).scalars().all()

    result = []
    for cat in cats:
        stats = db.execute(
            select(
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.amount), 0.0),
            ).where(Transaction.category_id == cat.id)
        ).one()
        result.append({
            "id": cat.id,
            "name": cat.name,
            "type": cat.category_type.value,
            "parent_id": cat.parent_id,
            "transaction_count": stats[0],
            "total_amount": round(float(stats[1]), 2),
        })
    return {"categories": result}


# ── Spending summaries ───────────────────────────────────────────────


@router.get("/spending/by-category")
def api_spending_by_category(
    months: int = Query(3, ge=1, le=60),
    account_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Spending breakdown by category for the last N months."""
    since = datetime.now() - timedelta(days=months * 30)

    q = (
        select(
            Category.name,
            Category.category_type,
            func.count(Transaction.id).label("count"),
            func.sum(Transaction.amount).label("total"),
            func.avg(Transaction.amount).label("avg"),
        )
        .join(Transaction.category)
        .where(Transaction.date >= since, Transaction.amount < 0)
    )
    if account_id:
        q = q.where(Transaction.account_id == account_id)

    rows = db.execute(
        q.group_by(Category.id).order_by(func.sum(Transaction.amount))
    ).all()

    return {
        "period_months": months,
        "since": since.strftime("%Y-%m-%d"),
        "categories": [
            {
                "name": r.name,
                "type": str(r.category_type),
                "transaction_count": r.count,
                "total_spent": round(float(r.total), 2),
                "avg_per_transaction": round(float(r.avg), 2),
            }
            for r in rows
        ],
    }


@router.get("/spending/monthly")
def api_spending_monthly(
    months: int = Query(12, ge=1, le=60),
    db: Session = Depends(get_db),
):
    """Monthly income vs spending totals for trend analysis."""
    since = datetime.now() - timedelta(days=months * 30)

    non_transfer_filter = (
        (Transaction.event_type.is_(None))
        | (~Transaction.event_type.in_([
            EconomicEventType.INTERNAL_TRANSFER.value,
            EconomicEventType.CARD_PAYMENT_SETTLEMENT.value,
        ]))
    )
    rows = db.execute(
        select(
            func.strftime("%Y-%m", Transaction.date).label("month"),
            func.sum(
                case(
                    (Transaction.amount < 0, Transaction.amount),
                    else_=0,
                )
            ).label("spending"),
            func.sum(
                case(
                    (Transaction.amount > 0, Transaction.amount),
                    else_=0,
                )
            ).label("income"),
            func.count(Transaction.id).label("count"),
        )
        .where(
            Transaction.date >= since,
            non_transfer_filter,
        )
        .group_by("month")
        .order_by("month")
    ).all()

    return {
        "period_months": months,
        "months": [
            {
                "month": r.month,
                "spending": round(float(r.spending), 2),
                "income": round(float(r.income), 2),
                "net": round(float(r.income) + float(r.spending), 2),
                "transaction_count": r.count,
            }
            for r in rows
        ],
    }


@router.get("/spending/top-merchants")
def api_top_merchants(
    months: int = Query(3, ge=1, le=60),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Top merchants/payees by total spend — helps agents identify
    recurring expenses and optimization opportunities."""
    since = datetime.now() - timedelta(days=months * 30)

    rows = db.execute(
        select(
            Transaction.description,
            func.count(Transaction.id).label("count"),
            func.sum(Transaction.amount).label("total"),
            func.min(Transaction.date).label("first_seen"),
            func.max(Transaction.date).label("last_seen"),
        )
        .where(
            Transaction.date >= since,
            Transaction.amount < 0,
            Transaction.is_transfer.is_(False),
        )
        .group_by(Transaction.description)
        .order_by(func.sum(Transaction.amount))
        .limit(limit)
    ).all()

    return {
        "period_months": months,
        "merchants": [
            {
                "description": r.description,
                "transaction_count": r.count,
                "total_spent": round(float(r.total), 2),
                "avg_per_transaction": round(
                    float(r.total) / r.count, 2
                ) if r.count else 0,
                "first_seen": r.first_seen.strftime("%Y-%m-%d"),
                "last_seen": r.last_seen.strftime("%Y-%m-%d"),
                "likely_recurring": r.count >= 3,
            }
            for r in rows
        ],
    }


# ── Net worth ────────────────────────────────────────────────────────


@router.get("/net-worth")
def api_net_worth(db: Session = Depends(get_db)):
    """Current net worth snapshot with full account breakdown."""
    nw = compute_net_worth(db)
    return {
        "date": nw.date.strftime("%Y-%m-%d"),
        "currency": nw.currency,
        "net_worth": round(nw.net_worth, 2),
        "total_assets": round(nw.total_assets, 2),
        "total_liabilities": round(nw.total_liabilities, 2),
        "breakdown": [
            {
                "account_id": b.account_id,
                "account_name": b.account_name,
                "type": b.account_type,
                "type_group": b.type_group,
                "balance": round(b.balance, 2),
                "native_currency": b.currency,
                "is_asset": b.is_asset,
            }
            for b in nw.breakdown
        ],
    }


@router.get("/net-worth/history")
def api_net_worth_history(
    months: int = Query(12, ge=1, le=120),
    db: Session = Depends(get_db),
):
    """Monthly net worth history for trend analysis."""
    series = compute_net_worth_series(db, months=months)
    return {
        "months": months,
        "snapshots": [
            {
                "date": s.date.strftime("%Y-%m-%d"),
                "net_worth": round(s.net_worth, 2),
                "total_assets": round(s.total_assets, 2),
                "total_liabilities": round(s.total_liabilities, 2),
            }
            for s in series.snapshots
        ],
    }


# ── Agent context endpoint ───────────────────────────────────────────


@router.get("/agent/context")
def api_agent_context(db: Session = Depends(get_db)):
    """Comprehensive context payload designed for LLM financial agents.

    Returns everything an agent needs in a single call: account overview,
    net worth, recent spending patterns, top expense categories,
    recurring merchants, and uncategorized transaction stats.
    """
    base = settings.base_currency
    now = datetime.now()
    three_months_ago = now - timedelta(days=90)
    one_month_ago = now - timedelta(days=30)

    # Accounts summary
    accounts = list_accounts(db)
    account_summaries = []
    for acct in accounts:
        bal = get_account_balance(db, acct.id, target_currency=base)
        account_summaries.append({
            "name": acct.name,
            "type": acct.account_type.value,
            "balance": round(bal, 2),
            "currency": acct.currency,
            "is_asset": acct.is_asset,
        })

    # Net worth
    nw = compute_net_worth(db)

    # Last 30-day spending by category
    cat_spending = db.execute(
        select(
            Category.name,
            func.sum(Transaction.amount).label("total"),
            func.count(Transaction.id).label("count"),
        )
        .join(Transaction.category)
        .where(
            Transaction.date >= one_month_ago,
            Transaction.amount < 0,
            Transaction.is_transfer.is_(False),
        )
        .group_by(Category.id)
        .order_by(func.sum(Transaction.amount))
    ).all()

    # Last 30-day income
    income_30d = db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
            Transaction.date >= one_month_ago,
            Transaction.amount > 0,
            Transaction.is_transfer.is_(False),
        )
    ).scalar() or 0.0

    spending_30d = db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
            Transaction.date >= one_month_ago,
            Transaction.amount < 0,
            Transaction.is_transfer.is_(False),
        )
    ).scalar() or 0.0

    # Uncategorized stats
    uncat_count = db.execute(
        select(func.count(Transaction.id)).where(
            Transaction.category_id.is_(None),
        )
    ).scalar() or 0

    total_txn_count = db.execute(
        select(func.count(Transaction.id))
    ).scalar() or 0

    # Top recurring merchants (3-month window)
    top_recurring = db.execute(
        select(
            Transaction.description,
            func.count(Transaction.id).label("count"),
            func.sum(Transaction.amount).label("total"),
        )
        .where(
            Transaction.date >= three_months_ago,
            Transaction.amount < 0,
            Transaction.is_transfer.is_(False),
        )
        .group_by(Transaction.description)
        .having(func.count(Transaction.id) >= 3)
        .order_by(func.sum(Transaction.amount))
        .limit(15)
    ).all()

    # Largest single transactions (last 30 days)
    largest_expenses = db.execute(
        select(Transaction)
        .where(
            Transaction.date >= one_month_ago,
            Transaction.amount < 0,
            Transaction.is_transfer.is_(False),
        )
        .order_by(Transaction.amount)
        .limit(10)
    ).scalars().all()

    return {
        "generated_at": now.isoformat(),
        "base_currency": base,
        "overview": {
            "net_worth": round(nw.net_worth, 2),
            "total_assets": round(nw.total_assets, 2),
            "total_liabilities": round(nw.total_liabilities, 2),
            "account_count": len(accounts),
            "total_transactions": total_txn_count,
            "uncategorized_transactions": uncat_count,
        },
        "accounts": account_summaries,
        "last_30_days": {
            "income": round(float(income_30d), 2),
            "spending": round(float(spending_30d), 2),
            "net_cashflow": round(float(income_30d) + float(spending_30d), 2),
            "spending_by_category": [
                {
                    "category": r.name,
                    "total": round(float(r.total), 2),
                    "count": r.count,
                }
                for r in cat_spending
            ],
        },
        "recurring_expenses": [
            {
                "description": r.description,
                "occurrences_3mo": r.count,
                "total_3mo": round(float(r.total), 2),
                "est_monthly": round(float(r.total) / 3, 2),
            }
            for r in top_recurring
        ],
        "largest_recent_expenses": [
            {
                "date": t.date.strftime("%Y-%m-%d"),
                "description": t.description,
                "amount": round(t.amount, 2),
                "category": t.category.name if t.category else None,
                "account": t.account.name if t.account else None,
            }
            for t in largest_expenses
        ],
        "agent_hints": {
            "data_quality": {
                "categorization_rate": round(
                    (1 - uncat_count / max(total_txn_count, 1)) * 100, 1
                ),
                "uncategorized_count": uncat_count,
            },
            "available_endpoints": [
                "GET /api/v1/accounts",
                "GET /api/v1/transactions?account_id=&category_id=&date_from=&date_to=&search=&is_transfer=&limit=&offset=",
                "GET /api/v1/categories",
                "GET /api/v1/spending/by-category?months=&account_id=",
                "GET /api/v1/spending/monthly?months=",
                "GET /api/v1/spending/top-merchants?months=&limit=",
                "GET /api/v1/net-worth",
                "GET /api/v1/net-worth/history?months=",
                "GET /api/v1/agent/context",
                "GET /api/v1/data-quality",
            ],
        },
    }


# ── Data quality ─────────────────────────────────────────────────────


@router.get("/data-quality")
def api_data_quality(db: Session = Depends(get_db)):
    """Blockers, warnings, and a derived close-readiness score.

    Agents should enumerate blockers/warnings rather than relying on the
    score alone — the score is a secondary convenience metric.
    """
    report = assess_quality(db)
    return {
        "blockers": report.blockers,
        "warnings": report.warnings,
        "close_readiness_score": round(report.close_readiness_score, 1),
        "as_of": report.as_of.isoformat(),
    }
