import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.templating import templates
from app.models.account import Account
from app.services.categorizer import categorize_batch
from app.services.import_service import import_transactions, preview_file
from app.services.ibkr_import import is_ibkr_file, parse_ibkr_csv, apply_ibkr_statement

router = APIRouter(prefix="/import", tags=["import"])


@router.post("/detect-account")
async def detect_account_endpoint(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Save the uploaded file temporarily, detect which account it belongs to,
    return JSON {account_id, confidence, reason} — called client-side before
    the user confirms the import account selection.
    """
    import tempfile, os
    from app.services.account_detector import detect_account

    ext = Path(file.filename).suffix.lower()
    if ext not in (".csv", ".xls", ".xlsx", ".pdf"):
        return {"account_id": None, "confidence": 0, "reason": "unsupported type"}

    # Write to a temp file so the detector can read it
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        accounts = db.execute(select(Account).order_by(Account.name)).scalars().all()
        account_id, confidence, reason = detect_account(tmp_path, file.filename, accounts)
    finally:
        os.unlink(tmp_path)

    return {
        "account_id": account_id,
        "confidence": round(confidence, 2),
        "reason": reason,
    }


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
    ext = Path(file.filename).suffix.lower()
    if ext not in (".csv", ".xls", ".xlsx", ".pdf"):
        return templates.TemplateResponse(request, "imports/upload.html", {
            "accounts": db.execute(select(Account).order_by(Account.name)).scalars().all(),
            "error": f"Unsupported file type '{ext}'. Please upload a CSV, XLS, XLSX, or PDF.",
        })

    upload_dir = Path(settings.upload_dir)
    dest = upload_dir / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Detect IBKR activity statement CSV — route to dedicated preview
    if ext == ".csv" and is_ibkr_file(str(dest)):
        try:
            parsed = parse_ibkr_csv(str(dest))
        except Exception as exc:
            return templates.TemplateResponse(request, "imports/upload.html", {
                "accounts": db.execute(select(Account).order_by(Account.name)).scalars().all(),
                "error": f"Failed to parse IBKR statement: {exc}",
            })
        account = db.get(Account, account_id)
        return templates.TemplateResponse(request, "imports/ibkr_preview.html", {
            "account_id": account_id,
            "account_name": account.name if account else f"Account {account_id}",
            "filepath": str(dest),
            "parsed": parsed,
        })

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
        "date_detection": preview.get("date_detection"),
    })


@router.post("/confirm")
def confirm_import(
    request: Request,
    account_id: int = Form(...),
    filepath: str = Form(...),
    col_date: str = Form(...),
    col_description: str = Form(...),
    col_amount: str = Form(""),
    col_debit: str = Form(""),
    col_credit: str = Form(""),
    col_balance: str = Form(""),
    col_currency: str = Form(""),
    date_format: str = Form("auto"),
    db: Session = Depends(get_db),
):
    mapping: dict[str, str] = {
        "date": col_date,
        "description": col_description,
    }
    if col_amount.strip():
        mapping["amount"] = col_amount
    if col_debit.strip():
        mapping["debit"] = col_debit
    if col_credit.strip():
        mapping["credit"] = col_credit
    if col_balance.strip():
        mapping["balance"] = col_balance
    if col_currency.strip():
        mapping["currency"] = col_currency

    dayfirst: bool | None = None
    if date_format == "dmy":
        dayfirst = True
    elif date_format == "mdy":
        dayfirst = False
    # "auto" leaves dayfirst=None → import_transactions will auto-detect

    from app.models.account import LIABILITY_TYPES, AccountType
    account = db.get(Account, account_id)
    acct_currency = account.currency if account else "USD"
    is_liability = account.account_type in LIABILITY_TYPES if account else False

    batch = import_transactions(
        db, account_id, filepath, mapping,
        account_currency=acct_currency,
        is_liability=is_liability,
        dayfirst=dayfirst,
    )

    # Auto-categorize the newly imported transactions
    cat_stats = categorize_batch(db, limit=batch.row_count + 100)

    # For mortgage PDFs, also extract and save loan metadata (balance, rate, payment)
    if account and account.account_type == AccountType.MORTGAGE and filepath.lower().endswith(".pdf"):
        try:
            from app.services.pdf_import import extract_mortgage_metadata
            meta = extract_mortgage_metadata(filepath)
            if meta:
                from datetime import datetime as _dt
                from decimal import Decimal as _Dec
                from app.models.snapshots import LiabilityBalanceSnapshot

                stmt_date = meta.get("statement_date") or _dt.now()

                if "outstanding_balance" in meta:
                    bal = meta["outstanding_balance"]
                    account.statement_balance = bal
                    account.statement_balance_as_of = stmt_date
                    account.balance_truth_source = "latest_statement"

                    # Write a dated snapshot so each statement upload is preserved.
                    # Deduplicate by (account_id, as_of_date) — same statement
                    # re-uploaded should overwrite rather than duplicate.
                    existing_snap = db.execute(
                        select(LiabilityBalanceSnapshot).where(
                            LiabilityBalanceSnapshot.account_id == account.id,
                            LiabilityBalanceSnapshot.as_of_date == stmt_date,
                        ).limit(1)
                    ).scalar_one_or_none()
                    if existing_snap is not None:
                        existing_snap.value_native = _Dec(str(bal))
                        existing_snap.source = "mortgage_statement_pdf"
                    else:
                        db.add(LiabilityBalanceSnapshot(
                            account_id=account.id,
                            as_of_date=stmt_date,
                            value_native=_Dec(str(bal)),
                            currency=account.currency or "USD",
                            source="mortgage_statement_pdf",
                            confidence=0.95,
                        ))

                if "interest_rate" in meta:
                    account.interest_rate = meta["interest_rate"]
                if "monthly_payment" in meta:
                    account.monthly_payment = meta["monthly_payment"]
                if "original_balance" in meta and not account.original_principal_balance:
                    account.original_principal_balance = meta["original_balance"]
                db.commit()

                # ── PaymentDecomposition for Regular Payment transactions ──
                payment_principal = meta.get("payment_principal")
                payment_interest  = meta.get("payment_interest")
                payment_escrow    = meta.get("payment_escrow", 0.0) or 0.0
                decomp_confidence = meta.get("_decomp_confidence", 0.80)

                if payment_principal is not None and payment_interest is not None:
                    from app.models.payment_decomposition import PaymentDecomposition
                    from app.models.enums import PaymentComponent
                    from app.models.transaction import Transaction as _Txn

                    # Find recently imported Regular Payment transactions for this account
                    reg_payments = db.execute(
                        select(_Txn)
                        .where(
                            _Txn.account_id == account.id,
                            _Txn.import_batch_id == batch.id,
                        )
                        .filter(_Txn.description.ilike("%regular payment%"))
                        .order_by(_Txn.date.desc())
                    ).scalars().all()

                    acct_currency = account.currency or "USD"

                    for txn in reg_payments:
                        # Dedup: skip if decomposition already exists for this txn
                        existing = db.execute(
                            select(PaymentDecomposition)
                            .where(PaymentDecomposition.transaction_id == txn.id)
                            .limit(1)
                        ).scalar_one_or_none()
                        if existing is not None:
                            continue

                        db.add(PaymentDecomposition(
                            transaction_id=txn.id,
                            component=PaymentComponent.PRINCIPAL.value,
                            amount=_Dec(str(payment_principal)),
                            currency=acct_currency,
                            provenance="imported",
                            confidence=decomp_confidence,
                        ))
                        db.add(PaymentDecomposition(
                            transaction_id=txn.id,
                            component=PaymentComponent.INTEREST.value,
                            amount=_Dec(str(payment_interest)),
                            currency=acct_currency,
                            provenance="imported",
                            confidence=decomp_confidence,
                        ))
                        if payment_escrow and payment_escrow > 0:
                            db.add(PaymentDecomposition(
                                transaction_id=txn.id,
                                component=PaymentComponent.ESCROW.value,
                                amount=_Dec(str(payment_escrow)),
                                currency=acct_currency,
                                provenance="imported",
                                confidence=decomp_confidence,
                            ))

                    db.commit()

                    # ── Transfer reconciliation: match bank outflow ────────
                    from app.models.reconciliation import ReconciliationGroup, ReconciliationMember
                    from app.models.transaction import Transaction as _Txn2
                    from app.models.account import Account as _Account
                    from sqlalchemy import and_, not_, exists, func as _func
                    import datetime as _datetime_mod

                    # Use recently imported mortgage transactions (current batch)
                    mortgage_txns = db.execute(
                        select(_Txn2)
                        .where(
                            _Txn2.account_id == account.id,
                            _Txn2.import_batch_id == batch.id,
                        )
                        .order_by(_Txn2.date.desc())
                        .limit(10)
                    ).scalars().all()

                    if mortgage_txns:
                        mortgage_total = sum(float(t.amount) for t in mortgage_txns)
                        earliest = min(t.date for t in mortgage_txns)
                        latest   = max(t.date for t in mortgage_txns)
                        date_lo  = earliest - _datetime_mod.timedelta(days=5)
                        date_hi  = latest   + _datetime_mod.timedelta(days=5)

                        # Find bank-side outflow NOT already in a reconciliation group
                        bank_txn = db.execute(
                            select(_Txn2)
                            .where(
                                _Txn2.account_id != account.id,
                                _Txn2.date >= date_lo,
                                _Txn2.date <= date_hi,
                                _Txn2.amount.between(
                                    _Dec(str(-mortgage_total - 1.0)),
                                    _Dec(str(-mortgage_total + 1.0)),
                                ),
                                not_(
                                    exists(
                                        select(ReconciliationMember.id)
                                        .where(ReconciliationMember.transaction_id == _Txn2.id)
                                    )
                                ),
                            )
                            .limit(1)
                        ).scalar_one_or_none()

                        if bank_txn:
                            grp = ReconciliationGroup(
                                group_type="mortgage_payment",
                                description=(
                                    f"Mortgage payment reconciliation — "
                                    f"{account.name} / {bank_txn.date.strftime('%Y-%m-%d')}"
                                ),
                                tolerance_base=_Dec("1.00"),
                                base_currency=acct_currency,
                                reconciliation_confidence=0.80,
                            )
                            db.add(grp)
                            db.flush()  # get grp.id

                            # Mortgage-side members (positive = inflow to loan balance)
                            for mt in mortgage_txns:
                                db.add(ReconciliationMember(
                                    group_id=grp.id,
                                    transaction_id=mt.id,
                                    allocated_amount_native=mt.amount,
                                    allocated_currency=acct_currency,
                                    allocated_amount_base=mt.amount,
                                    role="mortgage_inflow",
                                ))

                            # Bank-side member (negative = outflow from bank)
                            db.add(ReconciliationMember(
                                group_id=grp.id,
                                transaction_id=bank_txn.id,
                                allocated_amount_native=bank_txn.amount,
                                allocated_currency=bank_txn.original_currency or acct_currency,
                                allocated_amount_base=bank_txn.amount,
                                role="bank_outflow",
                            ))

                            db.commit()

        except Exception:
            pass  # metadata extraction is best-effort

    dupes = getattr(batch, "_duplicates_skipped", 0)
    return RedirectResponse(
        url=f"/accounts/{account_id}?imported={batch.row_count}"
            f"&duplicates={dupes}"
            f"&categorized={cat_stats['rules'] + cat_stats['keywords'] + cat_stats['llm']}",
        status_code=303,
    )


@router.post("/ibkr-confirm")
def ibkr_confirm(
    request: Request,
    account_id: int = Form(...),
    filepath: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        parsed = parse_ibkr_csv(filepath)
        stats = apply_ibkr_statement(db, account_id, parsed)
    except Exception as exc:
        accounts = db.execute(select(Account).order_by(Account.name)).scalars().all()
        return templates.TemplateResponse(request, "imports/upload.html", {
            "accounts": accounts,
            "error": f"IBKR import failed: {exc}",
        })

    positions = stats["positions_updated"]
    trades = stats["trades_added"]
    dividends = stats["dividends_added"]
    return RedirectResponse(
        url=f"/portfolio?ibkr_imported=1&positions={positions}&trades={trades}&dividends={dividends}",
        status_code=303,
    )
