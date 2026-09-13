import shutil
from datetime import datetime
from app.services.clock import naive_utc_now
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.auth import get_current_user
from app.models.user import User
from app.services.scoping import (
    get_owned_account_or_404, owned_account_ids, owned_accounts,
)
from app.templating import templates
from app.models.account import Account
from app.services.paycheck_service import (
    get_paycheck_summary,
    import_paycheck_stubs,
    list_paychecks,
    preview_paycheck_file,
)

router = APIRouter(prefix="/paychecks", tags=["paychecks"])


@router.get("", response_class=HTMLResponse)
def paychecks_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned = owned_account_ids(db, user)
    stubs = [st for st in list_paychecks(db) if st.account_id in owned]
    now = naive_utc_now()
    summary = get_paycheck_summary(db, year=now.year, account_ids=owned)
    accounts = sorted(owned_accounts(db, user), key=lambda a: a.name)

    return templates.TemplateResponse(request, "paychecks/list.html", {
        "stubs": stubs,
        "summary": summary,
        "current_year": now.year,
        "accounts": accounts,
    })


@router.get("/upload", response_class=HTMLResponse)
def paycheck_upload_form(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accounts = sorted(owned_accounts(db, user), key=lambda a: a.name)
    return templates.TemplateResponse(request, "paychecks/upload.html", {
        "accounts": accounts,
    })


@router.post("/upload")
async def paycheck_upload(
    request: Request,
    account_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.upload_safety import safe_upload_dest, UnsafeFilenameError
    try:
        dest = safe_upload_dest(settings.upload_dir, file.filename, user_id=user.id)
    except UnsafeFilenameError:
        return templates.TemplateResponse(request, "paychecks/upload.html", {
            "accounts": sorted(owned_accounts(db, user), key=lambda a: a.name),
            "error": "Invalid filename. Try renaming the file and upload again.",
        })
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    preview = preview_paycheck_file(str(dest))
    accounts = sorted(owned_accounts(db, user), key=lambda a: a.name)

    return templates.TemplateResponse(request, "paychecks/mapping.html", {
        "account_id": account_id,
        "filepath": str(dest),
        "columns": preview["columns"],
        "mapping": preview["mapping"],
        "preview": preview["preview"],
        "total_rows": preview["total_rows"],
        "accounts": accounts,
    })


@router.post("/confirm")
def paycheck_confirm_import(
    account_id: int = Form(...),
    filepath: str = Form(...),
    col_pay_date: str = Form(...),
    col_gross_pay: str = Form(...),
    col_net_pay: str = Form(...),
    col_federal_tax: str = Form(""),
    col_state_tax: str = Form(""),
    col_social_security: str = Form(""),
    col_medicare: str = Form(""),
    col_retirement_401k: str = Form(""),
    col_health_insurance: str = Form(""),
    col_employer: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mapping = {
        "pay_date": col_pay_date,
        "gross_pay": col_gross_pay,
        "net_pay": col_net_pay,
    }
    optional = {
        "federal_tax": col_federal_tax,
        "state_tax": col_state_tax,
        "social_security": col_social_security,
        "medicare": col_medicare,
        "retirement_401k": col_retirement_401k,
        "health_insurance": col_health_insurance,
        "employer": col_employer,
    }
    for k, v in optional.items():
        if v.strip():
            mapping[k] = v

    # The account and the file must both be this user's: the path is
    # form-supplied and would otherwise read any file the process can.
    get_owned_account_or_404(db, user, account_id)
    from app.services.upload_safety import assert_user_owns_path, UnsafeFilenameError
    try:
        assert_user_owns_path(settings.upload_dir, user.id, filepath)
    except UnsafeFilenameError:
        return HTMLResponse("File not found", status_code=404)
    count = import_paycheck_stubs(db, account_id, filepath, mapping)
    return RedirectResponse(
        url=f"/paychecks?imported={count}",
        status_code=303,
    )


@router.get("/manual", response_class=HTMLResponse)
def paycheck_manual_form(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accounts = sorted(owned_accounts(db, user), key=lambda a: a.name)
    return templates.TemplateResponse(request, "paychecks/manual.html", {
        "accounts": accounts,
    })


@router.post("/manual")
def paycheck_manual_create(
    account_id: int = Form(...),
    pay_date: str = Form(...),
    employer: str = Form(""),
    gross_pay: Decimal = Form(...),
    net_pay: Decimal = Form(...),
    federal_tax: Decimal = Form(Decimal("0.00")),
    state_tax: Decimal = Form(Decimal("0.00")),
    local_tax: Decimal = Form(Decimal("0.00")),
    social_security: Decimal = Form(Decimal("0.00")),
    medicare: Decimal = Form(Decimal("0.00")),
    retirement_401k: Decimal = Form(Decimal("0.00")),
    health_insurance: Decimal = Form(Decimal("0.00")),
    dental_insurance: Decimal = Form(Decimal("0.00")),
    vision_insurance: Decimal = Form(Decimal("0.00")),
    hsa_contribution: Decimal = Form(Decimal("0.00")),
    other_deductions: Decimal = Form(Decimal("0.00")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.paycheck_service import create_paycheck_manual

    get_owned_account_or_404(db, user, account_id)
    create_paycheck_manual(db, account_id, {
        "pay_date": datetime.strptime(pay_date, "%Y-%m-%d"),
        "employer": employer or None,
        "gross_pay": gross_pay,
        "net_pay": net_pay,
        "federal_tax": federal_tax,
        "state_tax": state_tax,
        "local_tax": local_tax,
        "social_security": social_security,
        "medicare": medicare,
        "retirement_401k": retirement_401k,
        "health_insurance": health_insurance,
        "dental_insurance": dental_insurance,
        "vision_insurance": vision_insurance,
        "hsa_contribution": hsa_contribution,
        "other_deductions": other_deductions,
    })
    return RedirectResponse(url="/paychecks", status_code=303)
