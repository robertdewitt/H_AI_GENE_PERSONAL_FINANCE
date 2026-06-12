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
from app.services.revolut_pdf_parser import (
    detect_revolut_sections,
    is_revolut_pdf,
    parse_revolut_pdf,
)

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

    from app.services.upload_safety import sanitize_filename, UnsafeFilenameError
    try:
        safe_name = sanitize_filename(file.filename)
    except UnsafeFilenameError:
        return {"account_id": None, "confidence": 0, "reason": "invalid filename"}
    ext = Path(safe_name).suffix.lower()
    if ext not in (".csv", ".xls", ".xlsx", ".pdf"):
        return {"account_id": None, "confidence": 0, "reason": "unsupported type"}

    # Write to a temp file so the detector can read it (NamedTemporaryFile's
    # OS-generated path is already inside the system temp dir — no client
    # data influences where it lands).
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        accounts = db.execute(select(Account).order_by(Account.name)).scalars().all()
        account_id, confidence, reason = detect_account(tmp_path, safe_name, accounts)
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
    from app.services.upload_safety import safe_upload_dest, UnsafeFilenameError
    try:
        dest = safe_upload_dest(settings.upload_dir, file.filename)
    except UnsafeFilenameError:
        return templates.TemplateResponse(request, "imports/upload.html", {
            "accounts": db.execute(select(Account).order_by(Account.name)).scalars().all(),
            "error": "Invalid filename. Try renaming the file and upload again.",
        })
    ext = dest.suffix.lower()
    if ext not in (".csv", ".xls", ".xlsx", ".pdf"):
        return templates.TemplateResponse(request, "imports/upload.html", {
            "accounts": db.execute(select(Account).order_by(Account.name)).scalars().all(),
            "error": f"Unsupported file type '{ext}'. Please upload a CSV, XLS, XLSX, or PDF.",
        })

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Detect Revolut GBP PDF statement
    if ext == ".pdf" and is_revolut_pdf(str(dest)):
        try:
            sections = detect_revolut_sections(str(dest))
        except Exception as exc:
            return templates.TemplateResponse(request, "imports/upload.html", {
                "accounts": db.execute(select(Account).order_by(Account.name)).scalars().all(),
                "error": f"Failed to read Revolut PDF: {exc}",
            })
        account = db.get(Account, account_id)
        return templates.TemplateResponse(request, "imports/revolut_preview.html", {
            "account_id": account_id,
            "account_name": account.name if account else f"Account {account_id}",
            "filepath": str(dest),
            "sections": sections,
        })

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

    # Match against scheduled payments and advance next_due_dates
    try:
        from app.services.scheduled_matcher import match_batch
        match_batch(db, batch.id)
    except Exception:
        pass  # matching is best-effort

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

    # ── Overdraft facility: pull from any UK bank statement PDF ──
    if account and filepath.lower().endswith(".pdf"):
        try:
            from app.services.pdf_import import extract_overdraft_facility
            from datetime import datetime as _dt
            from decimal import Decimal as _Dec
            od = extract_overdraft_facility(filepath)
            if od and od.get("overdraft_limit"):
                account.overdraft_limit = _Dec(str(od["overdraft_limit"]))
                stmt_date = od.get("statement_date")
                account.overdraft_as_of = (
                    _dt.combine(stmt_date, _dt.min.time())
                    if stmt_date is not None else _dt.now()
                )
                db.commit()
        except Exception:
            pass

    # ── Credit-card statement: extract balance, snapshot, scheduled payment,
    #    and sanity-check the imported transactions against the statement deltas ──
    stmt_warning: str | None = None
    if account and filepath.lower().endswith(".pdf"):
        from app.models.account import LIABILITY_TYPES
        if account.account_type in LIABILITY_TYPES:
            try:
                from app.services.pdf_import import extract_cc_metadata
                from app.models.scheduled_payment import ScheduledPayment
                from app.models.snapshots import LiabilityBalanceSnapshot
                from app.models.transaction import Transaction as _Txn
                from sqlalchemy import select as _sel, func as _func
                from datetime import datetime as _dt
                from decimal import Decimal as _Dec
                cc_meta = extract_cc_metadata(filepath) or {}
                new_bal  = cc_meta.get("new_balance")
                prev_bal = cc_meta.get("previous_balance")
                stmt_date = cc_meta.get("statement_date")
                due_date  = cc_meta.get("payment_due_date")
                min_pay   = cc_meta.get("minimum_payment")
                plan_due  = cc_meta.get("plan_it_due")
                plan_out  = cc_meta.get("plan_it_outstanding")

                # 1) Persist balance + snapshot whenever we can extract them.
                if new_bal is not None:
                    stmt_dt = (
                        _dt.combine(stmt_date, _dt.min.time())
                        if stmt_date is not None else _dt.now()
                    )
                    account.statement_balance = new_bal
                    account.statement_balance_as_of = stmt_dt
                    account.balance_truth_source = "latest_statement"

                    # Upsert a snapshot for this (account, as_of_date)
                    existing_snap = db.execute(
                        _sel(LiabilityBalanceSnapshot).where(
                            LiabilityBalanceSnapshot.account_id == account.id,
                            LiabilityBalanceSnapshot.as_of_date == stmt_dt,
                        ).limit(1)
                    ).scalar_one_or_none()
                    if existing_snap is not None:
                        existing_snap.value_native = _Dec(str(new_bal))
                        existing_snap.source = "cc_statement_pdf"
                        existing_snap.confidence = 1.0
                    else:
                        db.add(LiabilityBalanceSnapshot(
                            account_id=account.id,
                            as_of_date=stmt_dt,
                            value_native=_Dec(str(new_bal)),
                            value_base=_Dec(str(new_bal)),
                            currency=account.currency or "USD",
                            fx_rate=1.0,
                            source="cc_statement_pdf",
                            confidence=1.0,
                            stale_flag=False,
                        ))

                # 2) Sanity check: previous + sum(this batch's txns) should
                #    equal new_balance (sign convention varies by issuer, so
                #    accept either orientation). For BA Amex / Plan-It cards,
                #    plan_it_due is an aggregate that isn't represented as
                #    individual transactions, so subtract it from the
                #    expected change before comparing to the batch sum.
                if (
                    new_bal is not None
                    and prev_bal is not None
                    and batch is not None
                ):
                    batch_sum_raw = db.execute(
                        _sel(_func.coalesce(_func.sum(_Txn.amount), 0))
                        .where(_Txn.import_batch_id == batch.id)
                    ).scalar()
                    batch_sum = float(batch_sum_raw or 0)
                    expected_change = new_bal - prev_bal - float(plan_due or 0)
                    diff_a = abs(expected_change - batch_sum)
                    diff_b = abs(expected_change + batch_sum)
                    diff = min(diff_a, diff_b)
                    TOL = max(1.00, abs(new_bal) * 0.001)  # $1 or 0.1%, whichever bigger
                    if diff > TOL:
                        extra = f" (excl. plan-it {plan_due:.2f})" if plan_due else ""
                        stmt_warning = (
                            f"prev {prev_bal:.2f} + sum {batch_sum:.2f}{extra} "
                            f"≠ new {new_bal:.2f} (off by {diff:.2f})"
                        )

                # Persist Plan-It outstanding if extracted
                if plan_out is not None:
                    from datetime import datetime as _dt2
                    account.plan_it_balance = _Dec(str(plan_out))
                    account.plan_it_as_of = (
                        _dt.combine(stmt_date, _dt.min.time())
                        if stmt_date is not None else _dt2.now()
                    )

                # Replace per-plan detail rows for this account from the PDF
                # (delete-and-re-insert so completed plans drop off and
                # counters/balances reflect the latest snapshot).
                from app.services.pdf_import import extract_plan_it_plans
                from app.models.plan_it_plan import PlanItPlan
                plans = extract_plan_it_plans(filepath)
                if plans:
                    as_of_dt = (
                        _dt.combine(stmt_date, _dt.min.time())
                        if stmt_date is not None
                        else __import__("datetime").datetime.now()
                    )
                    db.execute(
                        _sel(PlanItPlan)
                        .where(PlanItPlan.account_id == account.id)
                    ).scalars().all()  # populate identity map so deletes cascade
                    from sqlalchemy import delete as _del
                    db.execute(
                        _del(PlanItPlan).where(PlanItPlan.account_id == account.id)
                    )
                    for p in plans:
                        if p.get("plan_total") is None:
                            continue
                        db.add(PlanItPlan(
                            account_id=account.id,
                            start_date=p.get("start_date"),
                            description=p["description"][:500],
                            plan_total=_Dec(str(p["plan_total"])),
                            plan_total_fee=(
                                _Dec(str(p["plan_total_fee"]))
                                if p.get("plan_total_fee") is not None else None
                            ),
                            balance_remaining=_Dec(str(p.get("balance_remaining") or 0)),
                            monthly_plan_amount=_Dec(str(p.get("monthly_plan_amount") or 0)),
                            monthly_fee=_Dec(str(p.get("monthly_fee") or 0)),
                            monthly_total=_Dec(str(p.get("monthly_total") or 0)),
                            instalment_number=int(p.get("instalment_number") or 0),
                            instalment_total=int(p.get("instalment_total") or 0),
                            as_of_date=as_of_dt,
                        ))

                # 3) Scheduled minimum payment, if we have a due date.
                if due_date is not None:
                    existing_sched = db.execute(
                        _sel(ScheduledPayment).where(
                            ScheduledPayment.account_id == account_id,
                            ScheduledPayment.source == "statement",
                            ScheduledPayment.active.is_(True),
                        ).limit(1)
                    ).scalar_one_or_none()
                    pay_amount = _Dec(str(-(min_pay or 0)))  # outflow = negative
                    if existing_sched is not None:
                        existing_sched.next_due_date = due_date
                        if min_pay is not None:
                            existing_sched.amount = pay_amount
                    elif min_pay is not None:
                        db.add(ScheduledPayment(
                            account_id=account_id,
                            description=f"{account.name} — Minimum Payment",
                            amount=pay_amount,
                            amount_type="estimated",
                            currency=account.currency or "USD",
                            frequency="monthly",
                            next_due_date=due_date,
                            day_of_month=due_date.day,
                            source="statement",
                            confidence=0.95,
                            active=True,
                        ))

                db.commit()
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "cc statement metadata extraction failed: %s", exc,
                )
                db.rollback()

    dupes = getattr(batch, "_duplicates_skipped", 0)
    url = (
        f"/accounts/{account_id}?imported={batch.row_count}"
        f"&duplicates={dupes}"
        f"&categorized={cat_stats['rules'] + cat_stats['keywords'] + cat_stats['llm']}"
    )
    if stmt_warning:
        import urllib.parse as _u
        url += f"&stmt_warning={_u.quote(stmt_warning)}"
    return RedirectResponse(url=url, status_code=303)


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


@router.post("/revolut-confirm")
def revolut_confirm(
    request: Request,
    account_id: int = Form(...),
    filepath: str = Form(...),
    sections: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    from decimal import Decimal
    from app.models.transaction import Transaction
    from app.models.import_batch import ImportBatch
    from app.models.account import Account, LIABILITY_TYPES

    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/import", status_code=303)

    include = set(sections) if sections else {"main"}

    try:
        txns = parse_revolut_pdf(filepath, include_sections=include)
    except Exception as exc:
        accounts = db.execute(select(Account).order_by(Account.name)).scalars().all()
        return templates.TemplateResponse(request, "imports/upload.html", {
            "accounts": accounts,
            "error": f"Revolut PDF import failed: {exc}",
        })

    # Build dedup set from existing transactions for this account
    from sqlalchemy import func as _func
    existing_keys: set[tuple] = set()
    for row in db.execute(
        select(Transaction.date, Transaction.description, Transaction.amount)
        .where(Transaction.account_id == account_id)
    ).all():
        existing_keys.add((
            row.date.strftime("%Y-%m-%d"),
            (row.description or "").strip().lower(),
            round(float(row.amount), 2),
        ))

    batch = ImportBatch(
        account_id=account_id,
        filename=Path(filepath).name,
        file_type="pdf",
        row_count=0,
        source="revolut_pdf",
    )
    db.add(batch)
    db.flush()

    imported = 0
    dupes = 0
    for t in txns:
        key = (
            t["date"].strftime("%Y-%m-%d"),
            t["description"].strip().lower(),
            round(float(t["amount"]), 2),
        )
        if key in existing_keys:
            dupes += 1
            continue
        existing_keys.add(key)

        txn = Transaction(
            account_id=account_id,
            date=t["date"],
            description=t["description"],
            amount=t["amount"],
            balance_after=t["balance"],
            import_batch_id=batch.id,
        )
        db.add(txn)
        imported += 1

    batch.row_count = imported
    db.commit()

    # Auto-categorize
    cat_stats = categorize_batch(db, limit=imported + 100)

    return RedirectResponse(
        url=f"/accounts/{account_id}?imported={imported}&duplicates={dupes}"
            f"&categorized={cat_stats['rules'] + cat_stats['keywords'] + cat_stats['llm']}",
        status_code=303,
    )
