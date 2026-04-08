import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.account import Account
from app.services.categorizer import categorize_batch
from app.services.import_service import import_transactions, preview_file

router = APIRouter(prefix="/import", tags=["import"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
def import_form(request: Request, db: Session = Depends(get_db)):
    accounts = db.execute(
        select(Account).order_by(Account.name)
    ).scalars().all()
    return templates.TemplateResponse(request, "imports/upload.html", {
        "accounts": accounts,
    })


@router.post("/upload")
async def upload_file(
    request: Request,
    account_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    upload_dir = Path(settings.upload_dir)
    dest = upload_dir / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    preview = preview_file(str(dest))
    accounts = db.execute(
        select(Account).order_by(Account.name)
    ).scalars().all()
    account = db.get(Account, account_id)

    return templates.TemplateResponse(request, "imports/mapping.html", {
        "account_id": account_id,
        "account_currency": account.currency if account else "USD",
        "filepath": str(dest),
        "columns": preview["columns"],
        "mapping": preview["mapping"],
        "preview": preview["preview"],
        "total_rows": preview["total_rows"],
        "accounts": accounts,
    })


@router.post("/confirm")
def confirm_import(
    request: Request,
    account_id: int = Form(...),
    filepath: str = Form(...),
    col_date: str = Form(...),
    col_description: str = Form(...),
    col_amount: str = Form(...),
    col_balance: str = Form(""),
    col_currency: str = Form(""),
    db: Session = Depends(get_db),
):
    mapping = {
        "date": col_date,
        "description": col_description,
        "amount": col_amount,
    }
    if col_balance.strip():
        mapping["balance"] = col_balance
    if col_currency.strip():
        mapping["currency"] = col_currency

    from app.models.account import LIABILITY_TYPES
    account = db.get(Account, account_id)
    acct_currency = account.currency if account else "USD"
    is_liability = account.account_type in LIABILITY_TYPES if account else False

    batch = import_transactions(
        db, account_id, filepath, mapping,
        account_currency=acct_currency,
        is_liability=is_liability,
    )

    # Auto-categorize the newly imported transactions
    cat_stats = categorize_batch(db, limit=batch.row_count + 100)

    return RedirectResponse(
        url=f"/accounts/{account_id}?imported={batch.row_count}"
            f"&categorized={cat_stats['rules'] + cat_stats['keywords'] + cat_stats['llm']}",
        status_code=303,
    )
