from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.category import Category
from app.models.transaction import Transaction
from app.services.net_worth_service import compute_net_worth, compute_net_worth_series
from app.templating import templates

router = APIRouter(prefix="/net-worth", tags=["net_worth"])

_PRESETS = [
    ("1y",  "1 Year"),
    ("ytd", "YTD"),
    ("2y",  "2 Years"),
    ("mtd", "MTD"),
    ("all", "All"),
]


def _preset_to_months(preset: str | None) -> int:
    today = date.today()
    if preset == "ytd":
        # months elapsed since Jan 1
        return max(1, (today.month - 1) + (1 if today.day > 1 else 0) + 1)
    if preset == "mtd":
        return 1
    if preset == "2y":
        return 24
    if preset == "all":
        return 120   # 10 years — effectively all data
    return 12        # default: 1 year


@router.get("", response_class=HTMLResponse)
def net_worth_page(
    request: Request,
    preset: str | None = Query(None),
    months: int | None = Query(None, ge=1, le=120),
    db: Session = Depends(get_db),
):
    # Preset takes priority; if neither set, default to 1y
    if preset is None and months is None:
        preset = "1y"
    resolved_months = months if months is not None else _preset_to_months(preset)

    current = compute_net_worth(db)
    series = compute_net_worth_series(db, months=resolved_months)

    groups: dict[str, Decimal] = {}
    for item in current.breakdown:
        groups.setdefault(item.type_group, Decimal("0.00"))
        groups[item.type_group] += item.balance

    # Spending by category across ALL accounts
    cat_rows = db.execute(
        select(
            Category.name,
            func.count(Transaction.id).label("txn_count"),
            func.sum(Transaction.amount).label("total"),
        )
        .join(Category, Transaction.category_id == Category.id)
        .group_by(Category.name)
        .order_by(func.sum(Transaction.amount))
    ).all()
    category_summary = [
        {"name": r.name, "count": r.txn_count, "total": r.total}
        for r in cat_rows
    ]

    uncategorized = db.execute(
        select(
            func.count(Transaction.id),
            func.sum(Transaction.amount),
        )
        .where(Transaction.category_id.is_(None))
    ).one()
    if uncategorized[0]:
        category_summary.append({
            "name": "Uncategorized",
            "count": uncategorized[0],
            "total": uncategorized[1] or 0,
        })

    return templates.TemplateResponse(request, "net_worth/dashboard.html", {
        "current": current,
        "series": series,
        "groups": groups,
        "months": resolved_months,
        "preset": preset or "",
        "presets": _PRESETS,
        "category_summary": category_summary,
    })


@router.get("/api/data")
def net_worth_api(
    months: int = Query(12, ge=1, le=120),
    db: Session = Depends(get_db),
):
    current = compute_net_worth(db)
    series = compute_net_worth_series(db, months=months)
    return {
        "current": current.model_dump(),
        "series": series.model_dump(),
    }
