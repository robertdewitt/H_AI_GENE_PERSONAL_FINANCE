"""JSON API for LLM agents and programmatic access.

All endpoints return structured JSON designed for consumption by LLM agents
that analyze spending habits, investments, and financial health.
"""
from datetime import datetime, timedelta
from app.services.clock import naive_utc_now
from decimal import Decimal
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
    get_many_account_balances_rich,
    list_accounts,
)
from app.services.data_quality import assess_quality
from app.services.net_worth_service import compute_net_worth, compute_net_worth_series
from app.services.spend_analysis import compute_spend_summary


def _year_month(col) -> any:
    """Return a dialect-aware year-month expression (YYYY-MM)."""
    if settings.db_backend == "postgresql":
        return func.to_char(col, "YYYY-MM")
    return func.strftime("%Y-%m", col)
from app.services.document_apply import (
    list_payroll_documents,
    list_property_pnl_series,
)
from app.models.rental_property import RentalProperty
from app.models.instrument import Instrument
from app.services.auto_reconciliation import create_suggested_transfer_groups
from app.services.attribution import attribute_nw_change

router = APIRouter(prefix="/api/v1", tags=["api"])


# ── Accounts ─────────────────────────────────────────────────────────


@router.get("/accounts")
def api_accounts(db: Session = Depends(get_db)):
    """All accounts with current balances in both native and base currency."""
    accounts = list_accounts(db)
    base = settings.base_currency

    # Batch balance computation — one GROUP BY instead of N queries
    balances_base = get_many_account_balances_rich(db, accounts, target_currency=base)

    # Batch transaction counts — one GROUP BY instead of N queries
    txn_counts: dict[int, int] = {}
    if accounts:
        for row in db.execute(
            select(Transaction.account_id, func.count(Transaction.id).label("cnt"))
            .where(Transaction.account_id.in_([a.id for a in accounts]))
            .group_by(Transaction.account_id)
        ).all():
            txn_counts[row.account_id] = row.cnt

    result = []
    for acct in accounts:
        base_result = balances_base.get(acct.id)
        base_bal = round(base_result.value, 2) if base_result else Decimal("0.00")
        result.append({
            "id": acct.id,
            "name": acct.name,
            "type": acct.account_type.value,
            "type_group": acct.type_group,
            "institution": acct.institution,
            "currency": acct.currency,
            "is_asset": acct.is_asset,
            "balance_base": base_bal,
            "base_currency": base,
            "transaction_count": txn_counts.get(acct.id, 0),
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
                func.coalesce(func.sum(Transaction.amount), 0),
            ).where(Transaction.category_id == cat.id)
        ).one()
        result.append({
            "id": cat.id,
            "name": cat.name,
            "type": cat.category_type.value,
            "parent_id": cat.parent_id,
            "transaction_count": stats[0],
            "total_amount": round(stats[1] or Decimal("0.00"), 2),
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
    since = naive_utc_now() - timedelta(days=months * 30)

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
                "total_spent": round(r.total or Decimal("0.00"), 2),
                "avg_per_transaction": round(r.avg or Decimal("0.00"), 2),
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
    since = naive_utc_now() - timedelta(days=months * 30)

    non_transfer_filter = (
        (Transaction.event_type.is_(None))
        | (~Transaction.event_type.in_([
            EconomicEventType.INTERNAL_TRANSFER.value,
            EconomicEventType.CARD_PAYMENT_SETTLEMENT.value,
        ]))
    )
    ym = _year_month(Transaction.date).label("month")
    rows = db.execute(
        select(
            ym,
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
        .group_by(ym)
        .order_by(ym)
    ).all()

    return {
        "period_months": months,
        "months": [
            {
                "month": r.month,
                "spending": round(r.spending or Decimal("0.00"), 2),
                "income": round(r.income or Decimal("0.00"), 2),
                "net": round((r.income or Decimal("0.00")) + (r.spending or Decimal("0.00")), 2),
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
    since = naive_utc_now() - timedelta(days=months * 30)

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
                "total_spent": round(r.total or Decimal("0.00"), 2),
                "avg_per_transaction": round(
                    (r.total or Decimal("0.00")) / r.count, 2
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
    net worth, true-spend breakdown (from splits — not raw amounts),
    recurring merchants, and data quality metrics.
    """
    base = settings.base_currency
    now = naive_utc_now()
    three_months_ago = now - timedelta(days=90)
    one_month_ago = now - timedelta(days=30)

    # Net worth — account balances are derived from nw.breakdown, no second pass needed
    nw = compute_net_worth(db)
    accounts = list_accounts(db)
    account_summaries = [
        {
            "name": b.account_name,
            "type": b.account_type,
            "balance": round(b.balance, 2),
            "currency": b.currency,
            "is_asset": b.is_asset,
        }
        for b in nw.breakdown
    ]

    # True spend from splits (not raw transaction amounts).
    # Falls back gracefully to zero totals when no splits exist yet.
    spend_summary = compute_spend_summary(db, months=1)
    income_30d = db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.date >= one_month_ago,
            Transaction.amount > 0,
            Transaction.event_type.notin_([
                EconomicEventType.INTERNAL_TRANSFER.value,
                EconomicEventType.CARD_PAYMENT_SETTLEMENT.value,
            ]),
        )
    ).scalar() or Decimal("0.00")

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
            "income": round(income_30d, 2),
            "true_spend": spend_summary.total_true_spend,
            "net_cashflow": round(income_30d + spend_summary.total_true_spend, 2),
            "spend_by_category": spend_summary.by_category,
            "spend_by_type": spend_summary.by_spend_type,
            "note": (
                "true_spend and spend_by_category are derived from split allocations "
                "and exclude transfers, principal payments, and investment contributions."
            ),
        },
        "recurring_expenses": [
            {
                "description": r.description,
                "occurrences_3mo": r.count,
                "total_3mo": round(r.total or Decimal("0.00"), 2),
                "est_monthly": round((r.total or Decimal("0.00")) / 3, 2),
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
            "spend_data_source": (
                "split_allocations" if spend_summary.total_true_spend != 0
                else "no_splits_yet — run POST /api/v1/reconciliation/auto-suggest "
                     "then re-import to populate splits"
            ),
        },
    }


# ── Data quality ─────────────────────────────────────────────────────


@router.get("/data-quality")
def api_data_quality(db: Session = Depends(get_db)):
    """Blockers, warnings, structured counters, and a derived score.

    Agents should enumerate blockers/warnings rather than relying on the
    score alone — the score is a secondary convenience metric.
    """
    report = assess_quality(db)
    c = report.counters
    return {
        "blockers": report.blockers,
        "warnings": report.warnings,
        "counters": {
            "uncategorized_count": c.uncategorized_count,
            "unclassified_count": c.unclassified_count,
            "low_confidence_count": c.low_confidence_count,
            "unresolved_reconciliation_count": c.unresolved_reconciliation_count,
            "stale_valuation_count": c.stale_valuation_count,
            "liabilities_without_decomposition": c.liabilities_without_decomposition,
            "missing_fx_count": c.missing_fx_count,
            "unsplit_transaction_count": c.unsplit_transaction_count,
            "reconciliation_fx_gap_count": c.reconciliation_fx_gap_count,
        },
        "close_readiness_score": round(report.close_readiness_score, 1),
        "as_of": report.as_of.isoformat(),
    }


# ── Balance sheet ────────────────────────────────────────────────────


@router.get("/balance-sheet")
def api_balance_sheet(db: Session = Depends(get_db)):
    """Full household balance sheet with per-account confidence and freshness."""
    base = settings.base_currency
    accounts = list_accounts(db)

    # Batch balance computation — eliminates N+1 DB roundtrips
    balances = get_many_account_balances_rich(db, accounts, target_currency=base)

    assets = []
    liabilities = []
    total_assets = Decimal("0.00")
    total_liabilities = Decimal("0.00")

    for acct in accounts:
        result = balances.get(acct.id)
        if result is None:
            continue
        entry = {
            "account_id": acct.id,
            "name": acct.name,
            "type": acct.account_type.value,
            "type_group": acct.type_group,
            "balance_base": round(result.value, 2),
            "currency": acct.currency,
            "base_currency": base,
            "balance_source": result.balance_source_used,
            "balance_as_of": result.balance_as_of.isoformat() if result.balance_as_of else None,
            "confidence": result.balance_confidence,
            "stale": result.balance_stale,
            "fx": {
                "pair": result.fx.fx_pair,
                "rate_date": result.fx.fx_rate_date.isoformat() if result.fx.fx_rate_date else None,
                "stale": result.fx.fx_stale,
            } if result.fx.fx_pair else None,
        }
        if acct.is_asset:
            assets.append(entry)
            total_assets += abs(result.value)
        else:
            liabilities.append(entry)
            total_liabilities += abs(result.value)

    return {
        "base_currency": base,
        "total_assets": round(total_assets, 2),
        "total_liabilities": round(total_liabilities, 2),
        "net_worth": round(total_assets - total_liabilities, 2),
        "assets": assets,
        "liabilities": liabilities,
    }


# ── Spend from splits ───────────────────────────────────────────────


@router.get("/spending/true-spend")
def api_true_spend(
    months: int = Query(3, ge=1, le=60),
    account_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """True spend analysis derived from split allocations ONLY.

    Distinguishes: lifestyle, fixed_core, debt_cost, tax, non_spend_cash_use.
    Raw transaction amounts are NOT used — only splits marked as true spend.
    """
    summary = compute_spend_summary(db, months=months, account_id=account_id)
    return {
        "period_months": summary.period_months,
        "total_true_spend": summary.total_true_spend,
        "by_spend_type": summary.by_spend_type,
        "by_category": summary.by_category,
        "monthly": [
            {
                "month": m.month,
                "spend_type": m.spend_type,
                "total": m.total,
                "count": m.count,
            }
            for m in summary.monthly
        ],
    }


# ── Structured documents (payroll, rental) ──────────────────────────


@router.get("/documents/payroll")
def api_payroll_documents(
    limit: int = Query(120, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Time series of payroll payslip documents (structured lines + metadata)."""
    docs = list_payroll_documents(db, limit=limit)
    return {
        "documents": [
            {
                "id": d.id,
                "statement_date": d.statement_date.strftime("%Y-%m-%d"),
                "period_start": d.period_start.strftime("%Y-%m-%d") if d.period_start else None,
                "period_end": d.period_end.strftime("%Y-%m-%d") if d.period_end else None,
                "employer": d.employer_or_counterparty,
                "currency": d.currency,
                "reference": d.reference,
                "split_validation_ok": d.split_validation_ok,
                "line_count": len(d.lines),
            }
            for d in docs
        ],
    }


@router.get("/rental-properties")
def api_rental_properties(db: Session = Depends(get_db)):
    """Rental property entities linked to statements and P&L."""
    rows = db.execute(select(RentalProperty).order_by(RentalProperty.name)).scalars().all()
    return {
        "properties": [
            {
                "id": p.id,
                "name": p.name,
                "code": p.code,
                "account_id": p.account_id,
            }
            for p in rows
        ],
    }


@router.get("/rental-properties/{property_id}/pnl")
def api_rental_property_pnl(
    property_id: int,
    limit: int = Query(120, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Property-level P&L snapshot time series (from rental statements)."""
    series = list_property_pnl_series(db, rental_property_id=property_id, limit=limit)
    return {
        "rental_property_id": property_id,
        "snapshots": [
            {
                "id": s.id,
                "statement_date": s.statement_date.strftime("%Y-%m-%d"),
                "period_start": s.period_start.strftime("%Y-%m-%d"),
                "period_end": s.period_end.strftime("%Y-%m-%d"),
                "currency": s.currency,
                "total_income": round(s.total_income, 2),
                "total_expense": round(s.total_expense, 2),
                "owner_draw": round(s.owner_draw, 2),
                "liability_adjustment": round(s.liability_adjustment, 2),
                "net_operating_income": round(s.net_operating_income, 2),
                "net_cash_flow": round(s.net_cash_flow, 2),
                "confidence": s.confidence,
                "stale": s.stale_flag,
            }
            for s in series
        ],
    }


# ── Instruments & positions (foundation) ────────────────────────────


@router.get("/instruments")
def api_instruments(db: Session = Depends(get_db)):
    """Listed securities / instruments (symbol-level pricing and lots)."""
    rows = db.execute(
        select(Instrument).order_by(Instrument.symbol)
    ).scalars().all()
    return {
        "instruments": [
            {
                "id": r.id,
                "symbol": r.symbol,
                "name": r.name,
                "currency": r.currency,
                "asset_class": r.asset_class,
            }
            for r in rows
        ],
    }


@router.post("/reconciliation/auto-suggest")
def api_reconciliation_auto_suggest(
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """Create suggested ReconciliationGroups for obvious transfer pairs."""
    n = create_suggested_transfer_groups(db, limit=limit)
    db.commit()
    return {"groups_created": n}


@router.get("/attribution/net-worth-change")
def api_attribution_net_worth_change(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Net worth change decomposition (flows + valuation diff + FX translation)."""
    try:
        start_d = datetime.strptime(start[:10], "%Y-%m-%d")
        end_d = datetime.strptime(end[:10], "%Y-%m-%d")
    except ValueError:
        return {"error": "Invalid date format; use YYYY-MM-DD"}
    result = attribute_nw_change(db, start_d, end_d)
    return {
        "period_start": result.period_start.isoformat() if result.period_start else None,
        "period_end": result.period_end.isoformat() if result.period_end else None,
        "nw_start": round(result.nw_start, 2),
        "nw_end": round(result.nw_end, 2),
        "delta_nw": round(result.delta_nw, 2),
        "unexplained": round(result.unexplained, 2),
        "warnings": result.warnings,
        "components": [
            {
                "label": c.label,
                "amount_base": round(c.amount_base, 2),
                "confidence": c.confidence,
                "notes": c.notes,
            }
            for c in result.components
        ],
    }
