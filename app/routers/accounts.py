from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.account import AccountType, LIABILITY_TYPES
from app.schemas.account import AccountCreate
from app.services.account_service import (
    create_account,
    delete_account,
    get_account,
    get_account_balance,
    get_accounts_grouped,
    get_transaction_count,
    list_accounts,
    update_account,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
def accounts_list(request: Request, db: Session = Depends(get_db)):
    groups = get_accounts_grouped(db)
    total_assets = sum(
        item["balance"]
        for items in groups.values()
        for item in items
        if item["account"].is_asset
    )
    total_liabilities = sum(
        abs(item["balance"])
        for items in groups.values()
        for item in items
        if not item["account"].is_asset
    )
    return templates.TemplateResponse(request, "accounts/list.html", {
        "groups": groups,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities,
    })


@router.get("/new", response_class=HTMLResponse)
def account_new_form(request: Request):
    return templates.TemplateResponse(request, "accounts/form.html", {
        "account": None,
        "account_types": list(AccountType),
        "liability_types": [t.value for t in LIABILITY_TYPES],
    })


@router.post("/new")
def account_create(
    request: Request,
    name: str = Form(...),
    account_type: str = Form(...),
    institution: str = Form(""),
    currency: str = Form("USD"),
    current_value: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    acct_type = AccountType(account_type)
    is_asset = acct_type not in LIABILITY_TYPES
    val = float(current_value) if current_value.strip() else None

    data = AccountCreate(
        name=name,
        account_type=acct_type,
        institution=institution or None,
        currency=currency,
        is_asset=is_asset,
        current_value=val,
        value_as_of_date=datetime.now() if val is not None else None,
        notes=notes or None,
    )
    create_account(db, data)
    return RedirectResponse(url="/accounts", status_code=303)


@router.get("/{account_id}", response_class=HTMLResponse)
def account_detail(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
):
    acct = get_account(db, account_id)
    if not acct:
        return HTMLResponse("Account not found", status_code=404)

    balance = get_account_balance(db, account_id)
    total_txn_count = get_transaction_count(db, account_id)

    from sqlalchemy import select as sa_select
    from sqlalchemy import func as sa_func
    from app.models.transaction import Transaction
    from app.models.category import Category

    recent_txns = db.execute(
        sa_select(Transaction)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.date.desc())
        .limit(50)
    ).scalars().all()

    # Category spending summary for this account
    cat_rows = db.execute(
        sa_select(
            Category.name,
            sa_func.count(Transaction.id).label("txn_count"),
            sa_func.sum(Transaction.amount).label("total"),
        )
        .join(Category, Transaction.category_id == Category.id)
        .where(Transaction.account_id == account_id)
        .group_by(Category.name)
        .order_by(sa_func.sum(Transaction.amount))
    ).all()
    category_summary = [
        {"name": r.name, "count": r.txn_count, "total": r.total}
        for r in cat_rows
    ]

    uncategorized = db.execute(
        sa_select(
            sa_func.count(Transaction.id),
            sa_func.sum(Transaction.amount),
        )
        .where(
            Transaction.account_id == account_id,
            Transaction.category_id.is_(None),
        )
    ).one()
    if uncategorized[0]:
        category_summary.append({
            "name": "Uncategorized",
            "count": uncategorized[0],
            "total": uncategorized[1] or 0,
        })

    return templates.TemplateResponse(request, "accounts/detail.html", {
        "account": acct,
        "balance": balance,
        "transactions": recent_txns,
        "total_transactions": total_txn_count,
        "category_summary": category_summary,
    })


@router.get("/{account_id}/edit", response_class=HTMLResponse)
def account_edit_form(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
):
    acct = get_account(db, account_id)
    if not acct:
        return HTMLResponse("Account not found", status_code=404)

    return templates.TemplateResponse(request, "accounts/form.html", {
        "account": acct,
        "account_types": list(AccountType),
        "liability_types": [t.value for t in LIABILITY_TYPES],
    })


@router.post("/{account_id}/edit")
def account_update(
    account_id: int,
    name: str = Form(...),
    account_type: str = Form(...),
    institution: str = Form(""),
    currency: str = Form("USD"),
    current_value: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    acct_type = AccountType(account_type)
    is_asset = acct_type not in LIABILITY_TYPES
    val = float(current_value) if current_value.strip() else None

    from app.schemas.account import AccountUpdate
    data = AccountUpdate(
        name=name,
        account_type=acct_type,
        institution=institution or None,
        currency=currency,
        is_asset=is_asset,
        current_value=val,
        value_as_of_date=datetime.now() if val is not None else None,
        notes=notes or None,
    )
    update_account(db, account_id, data)
    return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)


@router.post("/{account_id}/delete")
def account_remove(account_id: int, db: Session = Depends(get_db)):
    delete_account(db, account_id)
    return RedirectResponse(url="/accounts", status_code=303)
