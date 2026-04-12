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


@router.get("", response_class=HTMLResponse)
def net_worth_page(
    request: Request,
    months: int = Query(12, ge=1, le=120),
    db: Session = Depends(get_db),
):
    current = compute_net_worth(db)
    series = compute_net_worth_series(db, months=months)

    groups: dict[str, float] = {}
    for item in current.breakdown:
        groups.setdefault(item.type_group, 0.0)
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
        "months": months,
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
