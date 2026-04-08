from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction

router = APIRouter(prefix="/transactions", tags=["transactions"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _build_filters(
    account_id, category_id, date_from, date_to, search, is_transfer,
):
    """Build a list of WHERE clauses reusable for both count and data queries."""
    clauses = []
    if account_id:
        clauses.append(Transaction.account_id == account_id)
    if category_id:
        clauses.append(Transaction.category_id == category_id)
    if date_from:
        try:
            clauses.append(
                Transaction.date >= datetime.strptime(date_from, "%Y-%m-%d")
            )
        except ValueError:
            pass
    if date_to:
        try:
            clauses.append(
                Transaction.date <= datetime.strptime(date_to, "%Y-%m-%d")
            )
        except ValueError:
            pass
    if search:
        clauses.append(Transaction.description.ilike(f"%{search}%"))
    if is_transfer is not None:
        clauses.append(Transaction.is_transfer == is_transfer)
    return clauses


@router.get("", response_class=HTMLResponse)
def transactions_list(
    request: Request,
    account_id: int | None = Query(None),
    category_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    search: str | None = Query(None),
    is_transfer: bool | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    clauses = _build_filters(
        account_id, category_id, date_from, date_to, search, is_transfer,
    )

    total_count = db.execute(
        select(func.count(Transaction.id)).where(*clauses)
    ).scalar() or 0

    txns = db.execute(
        select(Transaction)
        .where(*clauses)
        .order_by(Transaction.date.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars().all()

    accounts = db.execute(
        select(Account).order_by(Account.name)
    ).scalars().all()
    categories = db.execute(
        select(Category).order_by(Category.name)
    ).scalars().all()

    total_pages = max(1, (total_count + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "transactions/list.html", {
        "transactions": txns,
        "accounts": accounts,
        "categories": categories,
        "filters": {
            "account_id": account_id,
            "category_id": category_id,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "search": search or "",
            "is_transfer": is_transfer,
        },
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
    })
