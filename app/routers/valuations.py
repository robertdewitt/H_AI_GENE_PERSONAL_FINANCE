from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates
from app.services.asset_valuation_service import (
    add_valuation,
    get_valuation_history,
    list_valuatable_accounts,
)

router = APIRouter(prefix="/valuations", tags=["valuations"])


@router.get("", response_class=HTMLResponse)
def valuations_page(request: Request, db: Session = Depends(get_db)):
    accounts = list_valuatable_accounts(db)
    account_data = []
    for acct in accounts:
        history = get_valuation_history(db, acct.id, limit=5)
        account_data.append({
            "account": acct,
            "history": history,
            "latest_value": acct.current_value,
            "latest_date": acct.value_as_of_date,
        })

    return templates.TemplateResponse(request, "valuations/list.html", {
        "account_data": account_data,
    })


@router.get("/{account_id}", response_class=HTMLResponse)
def valuation_detail(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
):
    from app.services.account_service import get_account
    account = get_account(db, account_id)
    if not account:
        return HTMLResponse("Account not found", status_code=404)

    history = get_valuation_history(db, account_id, limit=100)

    return templates.TemplateResponse(request, "valuations/detail.html", {
        "account": account,
        "history": history,
    })


@router.post("/{account_id}/add")
def valuation_add(
    account_id: int,
    value: float = Form(...),
    date: str = Form(...),
    currency: str = Form("USD"),
    source: str = Form("manual"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    add_valuation(
        db,
        account_id=account_id,
        date=datetime.strptime(date, "%Y-%m-%d"),
        value=value,
        currency=currency,
        source=source,
        notes=notes or None,
    )
    return RedirectResponse(url=f"/valuations/{account_id}", status_code=303)
