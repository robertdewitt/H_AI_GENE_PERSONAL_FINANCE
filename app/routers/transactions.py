from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.transfer_link import TransferLink
from app.services.categorizer import (
    categorize_batch,
    learn_from_correction,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _build_filters(
    account_id, category_id, date_from, date_to, search, is_transfer,
    amount_min, amount_max, currency, uncategorized,
):
    clauses = []
    if account_id:
        clauses.append(Transaction.account_id == account_id)
    if category_id:
        clauses.append(Transaction.category_id == category_id)
    if uncategorized:
        clauses.append(Transaction.category_id.is_(None))
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
    if amount_min is not None:
        clauses.append(Transaction.amount >= amount_min)
    if amount_max is not None:
        clauses.append(Transaction.amount <= amount_max)
    if currency:
        clauses.append(Transaction.original_currency == currency.upper())
    return clauses


@router.get("", response_class=HTMLResponse)
def transactions_list(
    request: Request,
    account_id: int | None = Query(None),
    category_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    search: str | None = Query(None),
    is_transfer: str | None = Query(None),
    amount_min: float | None = Query(None),
    amount_max: float | None = Query(None),
    currency: str | None = Query(None),
    uncategorized: bool | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    # Coerce is_transfer from string to bool | None
    transfer_flag = None
    if is_transfer == "true":
        transfer_flag = True
    elif is_transfer == "false":
        transfer_flag = False

    clauses = _build_filters(
        account_id, category_id, date_from, date_to, search, transfer_flag,
        amount_min, amount_max, currency, uncategorized,
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

    # Distinct currencies for filter dropdown
    currencies = db.execute(
        select(Transaction.original_currency)
        .distinct()
        .order_by(Transaction.original_currency)
    ).scalars().all()

    total_pages = max(1, (total_count + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "transactions/list.html", {
        "transactions": txns,
        "accounts": accounts,
        "categories": categories,
        "currencies": currencies,
        "filters": {
            "account_id": account_id,
            "category_id": category_id,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "search": search or "",
            "is_transfer": is_transfer or "",
            "amount_min": amount_min if amount_min is not None else "",
            "amount_max": amount_max if amount_max is not None else "",
            "currency": currency or "",
            "uncategorized": uncategorized,
        },
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
    })


# ── Single transaction edit ──────────────────────────────────────────

@router.get("/{txn_id}/edit", response_class=HTMLResponse)
def transaction_edit_form(
    request: Request,
    txn_id: int,
    db: Session = Depends(get_db),
):
    txn = db.get(Transaction, txn_id)
    if not txn:
        return HTMLResponse("Transaction not found", status_code=404)

    categories = db.execute(
        select(Category).order_by(Category.name)
    ).scalars().all()
    accounts = db.execute(
        select(Account).order_by(Account.name)
    ).scalars().all()

    return templates.TemplateResponse(request, "transactions/edit.html", {
        "txn": txn,
        "categories": categories,
        "accounts": accounts,
    })


@router.post("/{txn_id}/edit")
def transaction_update(
    txn_id: int,
    date: str = Form(...),
    description: str = Form(...),
    amount: float = Form(...),
    category_id: str = Form(""),
    is_transfer: bool = Form(False),
    transfer_account_id: str = Form(""),
    db: Session = Depends(get_db),
):
    txn = db.get(Transaction, txn_id)
    if not txn:
        return HTMLResponse("Transaction not found", status_code=404)

    old_category_id = txn.category_id
    txn.date = datetime.strptime(date, "%Y-%m-%d")
    txn.description = description
    txn.amount = amount
    txn.is_transfer = is_transfer

    new_cat_id = int(category_id) if category_id.strip() else None
    txn.category_id = new_cat_id

    # Learn from user correction if category changed
    if new_cat_id and new_cat_id != old_category_id:
        learn_from_correction(db, description, new_cat_id)

    # Handle transfer link
    if is_transfer and transfer_account_id.strip():
        dest_account_id = int(transfer_account_id)
        if dest_account_id != txn.account_id:
            _link_transfer(db, txn, dest_account_id)
    elif not is_transfer and txn.transfer_link_id:
        # Unlink if no longer a transfer
        txn.transfer_link_id = None

    db.commit()
    return RedirectResponse(
        url=f"/accounts/{txn.account_id}", status_code=303
    )


def _link_transfer(db: Session, txn: Transaction, other_account_id: int):
    """Find a matching transaction in the other account and link them.

    Uses a ±3-day date window and amount tolerance to handle clearing delays.
    """
    from datetime import timedelta

    from app.config import settings

    window = timedelta(days=settings.transfer_date_window_days)
    match = db.execute(
        select(Transaction).where(
            Transaction.account_id == other_account_id,
            Transaction.date >= txn.date - window,
            Transaction.date <= txn.date + window,
            func.abs(Transaction.amount + txn.amount) < 1.0,
            Transaction.transfer_link_id.is_(None),
        )
        .order_by(func.abs(Transaction.amount + txn.amount))
        .limit(1)
    ).scalar_one_or_none()

    if match:
        if txn.amount < 0:
            link = TransferLink(
                from_transaction_id=txn.id,
                to_transaction_id=match.id,
                amount=abs(txn.amount),
                date=txn.date,
                confidence=1.0,
                confirmed_by_user=True,
            )
        else:
            link = TransferLink(
                from_transaction_id=match.id,
                to_transaction_id=txn.id,
                amount=abs(txn.amount),
                date=txn.date,
                confidence=1.0,
                confirmed_by_user=True,
            )
        db.add(link)
        db.flush()
        txn.transfer_link_id = link.id
        txn.is_transfer = True
        match.transfer_link_id = link.id
        match.is_transfer = True


# ── Delete single transaction ────────────────────────────────────────

@router.post("/{txn_id}/delete")
def transaction_delete(txn_id: int, db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if not txn:
        return HTMLResponse("Transaction not found", status_code=404)
    account_id = txn.account_id
    db.delete(txn)
    db.commit()
    return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)


# ── Bulk operations ──────────────────────────────────────────────────

@router.post("/bulk/categorize")
def bulk_set_category(
    txn_ids: str = Form(...),
    category_id: int = Form(...),
    db: Session = Depends(get_db),
):
    ids = [int(x) for x in txn_ids.split(",") if x.strip().isdigit()]
    txns = db.execute(
        select(Transaction).where(Transaction.id.in_(ids))
    ).scalars().all()

    for txn in txns:
        old_cat = txn.category_id
        txn.category_id = category_id
        if category_id != old_cat:
            learn_from_correction(db, txn.description, category_id)

    db.commit()
    return RedirectResponse(url="/transactions", status_code=303)


@router.post("/bulk/delete")
def bulk_delete(
    txn_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    ids = [int(x) for x in txn_ids.split(",") if x.strip().isdigit()]
    db.execute(
        Transaction.__table__.delete().where(Transaction.id.in_(ids))
    )
    db.commit()
    return RedirectResponse(url="/transactions", status_code=303)


@router.post("/bulk/toggle-transfer")
def bulk_toggle_transfer(
    txn_ids: str = Form(...),
    is_transfer: bool = Form(True),
    db: Session = Depends(get_db),
):
    ids = [int(x) for x in txn_ids.split(",") if x.strip().isdigit()]
    txns = db.execute(
        select(Transaction).where(Transaction.id.in_(ids))
    ).scalars().all()
    for txn in txns:
        txn.is_transfer = is_transfer
    db.commit()
    return RedirectResponse(url="/transactions", status_code=303)


# ── Auto-categorize ─────────────────────────────────────────────────

@router.post("/auto-categorize")
def auto_categorize(
    request: Request,
    limit: int = Form(500),
    db: Session = Depends(get_db),
):
    stats = categorize_batch(db, limit=limit)
    return RedirectResponse(
        url=f"/transactions?auto_cat_total={stats['total']}"
            f"&auto_cat_rules={stats['rules']}"
            f"&auto_cat_kw={stats['keywords']}"
            f"&auto_cat_llm={stats['llm']}"
            f"&auto_cat_failed={stats['failed']}",
        status_code=303,
    )
