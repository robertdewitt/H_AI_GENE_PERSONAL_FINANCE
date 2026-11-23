import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from app.services.auth import get_current_user
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

from app.config import settings
from app.database import get_db
from app.services.clock import naive_utc_now
from app.models.user import User
from app.services.scoping import (
    owned_accounts, owner_of_account,
)
from app.templating import templates
from app.models.account import Account
from app.services.categorizer import categorize_batch
from app.services.import_service import import_transactions, preview_file
from app.services.ibkr_import import is_ibkr_file, parse_ibkr_csv, apply_ibkr_statement
from app.services.revolut_pdf_parser import (
    detect_revolut_sections,
    is_revolut_pdf,
    parse_revolut_pdf,
    revolut_statement_currency,
)
from app.services.rsu_service import (
    import_rsu_grants,
    is_merrill_rsu_csv,
    parse_merrill_rsu_csv,
)
from app.services.epa_pension_import import (
    import_pension_positions,
    is_epa_pension_pdf,
    parse_epa_pension_pdf,
)

router = APIRouter(prefix="/import", tags=["import"])


# ── Multi-file runs ────────────────────────────────────────────────────
#
# Several files can be uploaded at once. They are imported one after
# another: each goes through its usual review page in turn ("File 2 of 5"),
# and the rest of the run rides along in hidden form fields — ``queue``
# (files still to do), ``done`` (what happened to each one so far) and
# ``prev_mapping`` (the last column mapping confirmed). A spreadsheet with
# exactly the columns of the one just mapped is imported with that mapping
# without stopping, so a stack of exports from one bank needs one review.
# Every entry is JSON; paths travel as bare names inside the user's own
# upload directory and are re-checked on every step.

import json as _json
import re as _re
from urllib.parse import parse_qs as _parse_qs, urlsplit as _urlsplit

_STAMP = _re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{8}_")
_SHEET_EXTS = (".csv", ".xls", ".xlsx")


def _display_name(path) -> str:
    """The name the user picked, without the upload timestamp prefix."""
    return _STAMP.sub("", Path(str(path)).name)


def _load_entries(entries) -> list[dict]:
    out = []
    for e in entries or []:
        try:
            v = _json.loads(e)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def _run_ctx(queue: list[dict], done: list[dict], prev_mapping: dict | None) -> dict | None:
    """Template context for a page shown partway through a run, or None
    for an ordinary single-file import."""
    if not queue and not done:
        return None
    return {
        "queue": [_json.dumps(q) for q in queue],
        "done": [_json.dumps(d) for d in done],
        "prev_mapping": _json.dumps(prev_mapping) if prev_mapping else "",
        "position": len(done) + 1,
        "total": len(done) + 1 + len(queue),
    }


def _run_from_form(queue, done, prev_mapping) -> tuple[list[dict], list[dict], dict | None]:
    prev = None
    if prev_mapping:
        try:
            prev = _json.loads(prev_mapping)
        except ValueError:
            prev = None
    return _load_entries(queue), _load_entries(done), prev if isinstance(prev, dict) else None


def _result_from_url(name: str, account, url: str) -> dict:
    """What a finished import reported, read back off its redirect URL."""
    q = _parse_qs(_urlsplit(url).query)
    notes = q.get("warning", []) + q.get("stmt_warning", [])
    first = lambda k: (q.get(k) or [""])[0]
    return {
        "name": name,
        "account_id": account.id if account else None,
        "account_name": account.name if account else "",
        "status": "imported",
        "imported": first("imported"),
        "duplicates": first("duplicates"),
        "url": url,
        "notes": notes,
    }


def _upload_error(request, db, user, message: str, run: dict | None = None):
    return templates.TemplateResponse(request, "imports/upload.html", {
        "accounts": sorted(owned_accounts(db, user), key=lambda a: a.name),
        "error": message,
        "run": run,
    })


def _finish(request, db, user, url: str, *, filepath: str, account_id: int,
            queue, done, prev_mapping, mapping_used: dict | None = None):
    """End of one file's import: an ordinary import goes where it always
    did; inside a run, record the result and move on to the next file."""
    q, d, prev = _run_from_form(queue, done, prev_mapping)
    if not q and not d:
        return RedirectResponse(url=url, status_code=303)
    from app.services.scoping import get_owned_account
    d.append(_result_from_url(_display_name(filepath), get_owned_account(db, user, account_id), url))
    return _advance(request, db, user, q, d, mapping_used or prev)


def _advance(request, db, user, queue: list[dict], done: list[dict], prev: dict | None):
    """Take the next file of the run: import it straight away when it has
    the columns just mapped, otherwise show its review page. With nothing
    left, show what happened to each file."""
    from app.services.scoping import get_owned_account
    from app.services.upload_safety import (
        UnsafeFilenameError, assert_user_owns_path, user_upload_dir,
    )
    while queue:
        item = queue.pop(0)
        name = str(item.get("name") or "")
        shown = _display_name(name)
        try:
            path = assert_user_owns_path(
                settings.upload_dir, user.id, user_upload_dir(settings.upload_dir, user.id) / Path(name).name,
            )
        except UnsafeFilenameError:
            done.append({"name": shown, "status": "failed", "notes": ["The uploaded file could not be found."]})
            continue
        account = get_owned_account(db, user, int(item.get("account_id") or 0))
        if account is None:
            done.append({"name": shown, "status": "failed", "notes": ["No account was chosen for this file."]})
            continue

        ext = path.suffix.lower()
        if (prev and ext in _SHEET_EXTS
                and not (ext == ".csv" and (is_merrill_rsu_csv(str(path)) or is_ibkr_file(str(path))))):
            try:
                same = preview_file(str(path))["columns"] == prev.get("columns")
            except Exception:
                same = False
            if same:
                m = prev.get("fields", {})
                url = _confirm_core(
                    db, account.id, str(path),
                    col_date=m.get("col_date", ""), col_description=m.get("col_description", ""),
                    col_amount=m.get("col_amount", ""), col_debit=m.get("col_debit", ""),
                    col_credit=m.get("col_credit", ""), col_balance=m.get("col_balance", ""),
                    col_currency=m.get("col_currency", ""), date_format=m.get("date_format", "auto"),
                )
                result = _result_from_url(shown, account, url)
                result["auto"] = True
                done.append(result)
                continue
        return _preview_for(request, db, user, account.id, path, _run_ctx(queue, done, prev))

    return templates.TemplateResponse(request, "imports/summary.html", {"results": done})


@router.post("/detect-account")
async def detect_account_endpoint(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
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
        accounts = sorted(owned_accounts(db, user), key=lambda a: a.name)
        account_id, confidence, reason = detect_account(tmp_path, safe_name, accounts)
    finally:
        os.unlink(tmp_path)

    return {
        "account_id": account_id,
        "confidence": round(confidence, 2),
        "reason": reason,
    }


@router.get("", response_class=HTMLResponse)
def import_form(request: Request, account: int | None = None,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The upload page. ``?account=`` (the account page's Import button)
    preselects that account; detection then only suggests a different one."""
    accounts = sorted(owned_accounts(db, user), key=lambda a: a.name)
    selected = account if account in {a.id for a in accounts} else None
    return templates.TemplateResponse(request, "imports/upload.html", {
        "accounts": accounts,
        "selected_account_id": selected,
    })


@router.post("/upload")
async def upload_file(
    request: Request,
    account_id: int | None = Form(None),
    file: UploadFile | None = File(None),
    files: list[UploadFile] = File(default=[]),
    file_account_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Save the uploaded file(s), then show the first one's review page.

    One file is the classic flow (``file`` + ``account_id``). Several files
    (``files``, each with its own entry in ``file_account_ids``, falling back
    to ``account_id``) start a run — see _advance."""
    from app.services.scoping import get_owned_account
    from app.services.upload_safety import (
        UnsafeFilenameError, UploadTooLarge, copy_with_limit, safe_upload_dest,
    )
    uploads = [f for f in ([file] if file is not None else []) + list(files) if f is not None and f.filename]
    if not uploads:
        return _upload_error(request, db, user, "Choose a file to import.")

    saved: list[tuple[Path, int]] = []
    for i, up in enumerate(uploads):
        acct_id = file_account_ids[i] if i < len(file_account_ids) and file_account_ids[i] else account_id
        label = f"{up.filename}: " if len(uploads) > 1 else ""
        if not acct_id or get_owned_account(db, user, acct_id) is None:
            return _upload_error(request, db, user, f"{label}choose the account this file belongs to.")
        try:
            dest = safe_upload_dest(settings.upload_dir, up.filename, user_id=user.id)
        except UnsafeFilenameError:
            return _upload_error(request, db, user,
                                 f"{label}invalid filename. Try renaming the file and upload again.")
        ext = dest.suffix.lower()
        if ext not in (".csv", ".xls", ".xlsx", ".pdf"):
            return _upload_error(request, db, user,
                                 f"{label}unsupported file type '{ext}'. Please upload a CSV, XLS, XLSX, or PDF.")
        try:
            copy_with_limit(up.file, dest, settings.max_upload_bytes)
        except UploadTooLarge:
            limit_mb = settings.max_upload_bytes // (1024 * 1024)
            return _upload_error(request, db, user, (
                f"{label}that file is larger than the {limit_mb} MB upload limit. "
                "A statement export should be well under that."
            ))
        saved.append((dest, acct_id))

    if len(saved) == 1:
        dest, acct_id = saved[0]
        return _preview_for(request, db, user, acct_id, dest, None)
    queue = [{"name": d.name, "account_id": a} for d, a in saved]
    return _advance(request, db, user, queue, [], None)


@router.post("/skip")
def skip_file(
    request: Request,
    filepath: str = Form(""),
    queue: list[str] = Form(default=[]),
    done: list[str] = Form(default=[]),
    prev_mapping: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Leave the current file of a run unimported and go on to the next."""
    q, d, prev = _run_from_form(queue, done, prev_mapping)
    if filepath:
        d.append({"name": _display_name(filepath), "status": "skipped"})
    return _advance(request, db, user, q, d, prev)


def _preview_for(request, db, user, account_id: int, dest: Path, run: dict | None):
    """The review page for one saved upload — the right preview for the kind
    of file it is, or the column mapping for an ordinary statement."""
    ext = dest.suffix.lower()

    # Detect Revolut GBP PDF statement
    if ext == ".pdf" and is_revolut_pdf(str(dest)):
        try:
            sections = detect_revolut_sections(str(dest))
        except Exception as exc:
            return _upload_error(request, db, user, f"Failed to read Revolut PDF: {exc}", run)
        account = db.get(Account, account_id)
        # Revolut exports one file per currency and names them by hash, so
        # a USD statement lands on the GBP account by a slip of the mouse —
        # 81 dollar rows once went into the pound account and 606 pound
        # rows into the dollar one. The header says which it is.
        stmt_ccy = revolut_statement_currency(str(dest))
        if account is not None and stmt_ccy and (account.currency or "").upper() != stmt_ccy:
            matching = [a for a in owned_accounts(db, user) if (a.currency or "").upper() == stmt_ccy]
            hint = (f" Accounts in {stmt_ccy}: " + ", ".join(a.name for a in matching) + "."
                    if matching else "")
            return _upload_error(request, db, user, (
                    f"This is a {stmt_ccy} statement but {account.name} is a "
                    f"{account.currency} account. Pick the {stmt_ccy} account.{hint}"
                ), run)
        return templates.TemplateResponse(request, "imports/revolut_preview.html", {
            "run": run,
            "account_id": account_id,
            "account_name": account.name if account else f"Account {account_id}",
            "filepath": str(dest),
            "sections": sections,
            "statement_currency": stmt_ccy,
        })

    # Detect Merrill / BofA RSU award-summary CSV — dedicated preview
    if ext == ".csv" and is_merrill_rsu_csv(str(dest)):
        try:
            parsed_rsu = parse_merrill_rsu_csv(str(dest))
        except Exception as exc:
            return _upload_error(request, db, user, f"Failed to parse RSU award summary: {exc}", run)
        account = db.get(Account, account_id)
        return templates.TemplateResponse(request, "imports/rsu_preview.html", {
            "run": run,
            "account_id": account_id,
            "account_name": account.name if account else f"Account {account_id}",
            "filepath": str(dest),
            "parsed": parsed_rsu,
        })

    # A PDF with no text layer is a screenshot or a scan. Every parser below
    # reads extracted text, so without this the upload silently imports
    # nothing and looks like it did nothing at all.
    if ext == ".pdf":
        from app.services.vision_extract import (
            extract_pension_balances, has_text_layer, vision_model_available,
        )
        if not has_text_layer(str(dest)):
            parsed_pension = extract_pension_balances(str(dest))
            if parsed_pension is not None and parsed_pension.is_valid:
                account = db.get(Account, account_id)
                return templates.TemplateResponse(request, "imports/epa_preview.html", {
                    "run": run,
                    "account_id": account_id,
                    "account_name": account.name if account else f"Account {account_id}",
                    "filepath": str(dest),
                    "parsed": parsed_pension,
                    "from_image": True,
                })
            if vision_model_available():
                detail = (
                    "A local vision model read the page but the figures did not "
                    "reconcile — units x unit price must match each fund value, "
                    "and the funds must sum to the stated total. Nothing was "
                    "imported."
                )
            else:
                detail = (
                    "No local vision model is installed, so there is nothing "
                    f"that can read it. Install one (ollama pull "
                    f"{settings.ollama_vision_model}) or enter the values "
                    "manually on the account page."
                )
            return _upload_error(request, db, user, (
                    "This PDF contains no text — it looks like a screenshot or "
                    f"a scan. {detail}"
                ), run)

    # Detect WTW ePA pension "My Fund Balance" PDF — dedicated preview
    if ext == ".pdf" and is_epa_pension_pdf(str(dest)):
        try:
            parsed_pension = parse_epa_pension_pdf(str(dest))
        except Exception as exc:
            return _upload_error(request, db, user, f"Failed to parse ePA pension statement: {exc}", run)
        account = db.get(Account, account_id)
        return templates.TemplateResponse(request, "imports/epa_preview.html", {
            "run": run,
            "account_id": account_id,
            "account_name": account.name if account else f"Account {account_id}",
            "filepath": str(dest),
            "parsed": parsed_pension,
        })

    # Detect IBKR activity statement CSV — route to dedicated preview
    if ext == ".csv" and is_ibkr_file(str(dest)):
        try:
            parsed = parse_ibkr_csv(str(dest))
        except Exception as exc:
            return _upload_error(request, db, user, f"Failed to parse IBKR statement: {exc}", run)
        account = db.get(Account, account_id)
        return templates.TemplateResponse(request, "imports/ibkr_preview.html", {
            "run": run,
            "account_id": account_id,
            "account_name": account.name if account else f"Account {account_id}",
            "filepath": str(dest),
            "parsed": parsed,
        })

    preview = preview_file(str(dest))
    accounts = sorted(owned_accounts(db, user), key=lambda a: a.name)
    account = db.get(Account, account_id)

    return templates.TemplateResponse(request, "imports/mapping.html", {
        "run": run,
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
    queue: list[str] = Form(default=[]),
    done: list[str] = Form(default=[]),
    prev_mapping: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    # Filepath came from a hidden form field — verify it's a real file
    # inside *this* user's upload directory before we open it. Without
    # this check a logged-in attacker could pass any other user's recent
    # upload path and trigger an import into their own account.
    from app.services.upload_safety import assert_user_owns_path, UnsafeFilenameError
    try:
        assert_user_owns_path(settings.upload_dir, user.id, filepath)
    except UnsafeFilenameError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    fields = {
        "col_date": col_date, "col_description": col_description,
        "col_amount": col_amount, "col_debit": col_debit, "col_credit": col_credit,
        "col_balance": col_balance, "col_currency": col_currency,
        "date_format": date_format,
    }
    url = _confirm_core(db, account_id, filepath, **fields)
    mapping_used = None
    if Path(filepath).suffix.lower() in _SHEET_EXTS:
        try:
            mapping_used = {"columns": preview_file(filepath)["columns"], "fields": fields}
        except Exception:
            mapping_used = None
    return _finish(request, db, user, url, filepath=filepath, account_id=account_id,
                   queue=queue, done=done, prev_mapping=prev_mapping, mapping_used=mapping_used)


def _confirm_core(
    db: Session, account_id: int, filepath: str, *,
    col_date: str, col_description: str, col_amount: str = "", col_debit: str = "",
    col_credit: str = "", col_balance: str = "", col_currency: str = "",
    date_format: str = "auto",
) -> str:
    """Import one mapped file and run everything that follows an import;
    returns the account-page URL that reports the result. The caller has
    already checked the file is the user's."""
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

    # Auto-categorize the newly imported transactions — only those. With a
    # bare limit this re-ran the model over every old uncategorised row in
    # the ledger on each import, 209 of them, none of which were new.
    cat_stats = categorize_batch(
        db, transaction_ids=[t.id for t in batch.transactions],
    )

    # Match against scheduled payments and advance next_due_dates.
    # Best-effort — a matching failure must not block the import, but it
    # also must not be silent: log it and surface a warning so the user
    # can investigate why the post-import banner doesn't show matches.
    extraction_warnings: list[str] = []
    try:
        from app.services.scheduled_matcher import backfill_matches, match_batch
        match_batch(db, batch.id)
        # The batch pass only sees this file's rows. Sweep the account too, so
        # a schedule left behind by an earlier import or a manual entry
        # catches up instead of staying overdue.
        backfill_matches(db, account_id=account_id)
        db.commit()
        # A loan payment posted in parts (regular + extra principal) is
        # linked to the single outflow that paid it once both sides are in.
        from app.services.loan_payment_linking import link_all_loan_payments
        link_all_loan_payments(db, user_id=owner_of_account(db, account_id))
        db.commit()
        # A card payment seen only at the bank goes onto the card it paid.
        from app.services.card_payment_mirror import mirror_card_payments
        mirror_card_payments(db, user_id=owner_of_account(db, account_id))
    except Exception as exc:
        log.warning(
            "scheduled_matcher.match_batch failed for batch %s: %s",
            batch.id, exc, exc_info=True,
        )
        extraction_warnings.append(
            f"Scheduled-payment matching failed: {exc}"
        )

    # For mortgage PDFs, also extract and save loan metadata (balance, rate, payment)
    if account and account.account_type == AccountType.MORTGAGE and filepath.lower().endswith(".pdf"):
        try:
            from app.services.pdf_import import extract_mortgage_metadata
            meta = extract_mortgage_metadata(filepath)
            if meta:
                from datetime import datetime as _dt
                from decimal import Decimal as _Dec
                from app.models.snapshots import LiabilityBalanceSnapshot

                stmt_date = meta.get("statement_date") or naive_utc_now()

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

                # Regular mortgage payment → statement-sourced scheduled payment
                # so it shows up on the tasks page and feeds the cash-flow
                # forecast. Mortgage statements rarely print a "due date", so
                # anchor the next instalment one month past the statement date
                # (rolled forward to the future), letting the import matcher
                # snap it to the real payment date later.
                mp = meta.get("monthly_payment")
                if mp:
                    from app.models.scheduled_payment import ScheduledPayment as _Sched
                    from app.services.recurring_detector import _add_months as _addm
                    anchor = stmt_date.date() if hasattr(stmt_date, "date") else stmt_date
                    next_due = _addm(anchor, 1)
                    today_d = naive_utc_now().date()
                    while next_due < today_d:
                        next_due = _addm(next_due, 1)
                    account.payment_due_date = next_due
                    pay_amt = _Dec(str(-abs(float(mp))))
                    existing_mp = db.execute(
                        select(_Sched).where(
                            _Sched.account_id == account.id,
                            _Sched.source == "statement",
                            _Sched.active.is_(True),
                        ).limit(1)
                    ).scalar_one_or_none()
                    from app.services.scheduled_dismissal import (
                        is_dismissed as _is_dismissed,
                    )
                    _mp_desc = f"{account.name} — Mortgage Payment"
                    if existing_mp is not None:
                        existing_mp.amount = pay_amt
                        existing_mp.next_due_date = next_due
                        existing_mp.day_of_month = next_due.day
                    elif _is_dismissed(db, account.id, _mp_desc):
                        pass   # deleted by the user — don't rebuild it
                    else:
                        db.add(_Sched(
                            account_id=account.id,
                            description=_mp_desc,
                            amount=pay_amt,
                            amount_type="fixed",
                            currency=account.currency or "USD",
                            frequency="monthly",
                            next_due_date=next_due,
                            day_of_month=next_due.day,
                            source="statement",
                            confidence=0.90,
                            active=True,
                        ))
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

        except Exception as exc:
            log.warning(
                "extract_mortgage_metadata failed for %s: %s",
                filepath, exc, exc_info=True,
            )
            extraction_warnings.append(
                f"Mortgage metadata could not be extracted: {exc}"
            )

    # ── Overdraft facility: pull from any UK bank statement PDF ──
    if account and filepath.lower().endswith(".pdf"):
        from app.services.pdf_import import extract_overdraft_facility
        from datetime import datetime as _dt
        from decimal import Decimal as _Dec
        od = None
        try:
            od = extract_overdraft_facility(filepath)
        except Exception as exc:
            log.warning(
                "extract_overdraft_facility failed for %s: %s",
                filepath, exc, exc_info=True,
            )
            extraction_warnings.append(
                f"Overdraft facility could not be read from this PDF: {exc}"
            )
        # Persist outside the try so a write failure surfaces normally
        # (and so no db.commit() is hidden inside a swallowed except).
        if od and od.get("overdraft_limit"):
            account.overdraft_limit = _Dec(str(od["overdraft_limit"]))
            stmt_date = od.get("statement_date")
            account.overdraft_as_of = (
                _dt.combine(stmt_date, _dt.min.time())
                if stmt_date is not None else naive_utc_now()
            )
            db.commit()

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
                # Fall through to UK HP loan extraction when the file isn't
                # a credit-card statement — covers Black Horse / Tesla
                # Finance and similar lender PDFs the CC regex doesn't fit.
                if not cc_meta.get("new_balance"):
                    from app.services.pdf_import import extract_loan_metadata
                    loan_meta = extract_loan_metadata(filepath) or {}
                    for k in (
                        "new_balance", "previous_balance",
                        "statement_date", "interest_rate",
                        "amount_of_credit",
                    ):
                        if k in loan_meta and loan_meta[k] is not None:
                            cc_meta.setdefault(k, loan_meta[k])
                new_bal  = cc_meta.get("new_balance")
                prev_bal = cc_meta.get("previous_balance")
                stmt_date = cc_meta.get("statement_date")
                due_date  = cc_meta.get("payment_due_date")
                min_pay   = cc_meta.get("minimum_payment")
                plan_due  = cc_meta.get("plan_it_due")
                plan_out  = cc_meta.get("plan_it_outstanding")
                loan_rate = cc_meta.get("interest_rate")
                loan_principal = cc_meta.get("amount_of_credit")

                # 0) The card's credit limit, from the newest statement that
                #    states one (an older statement imported later must not
                #    roll it back).
                limit = cc_meta.get("credit_limit")
                if limit is not None and account.account_type == AccountType.CREDIT_CARD:
                    lim_dt = (
                        _dt.combine(stmt_date, _dt.min.time())
                        if stmt_date is not None else naive_utc_now()
                    )
                    if account.credit_limit_as_of is None or lim_dt >= account.credit_limit_as_of:
                        account.credit_limit = _Dec(str(limit))
                        account.credit_limit_as_of = lim_dt

                # 1) Persist balance + snapshot whenever we can extract them.
                if new_bal is not None:
                    stmt_dt = (
                        _dt.combine(stmt_date, _dt.min.time())
                        if stmt_date is not None else naive_utc_now()
                    )
                    account.statement_balance = new_bal
                    account.statement_balance_as_of = stmt_dt
                    # A card keeps moving between statements, so anchor on the
                    # statement and roll it forward with each transaction
                    # (hybrid). Loans stay on the statement figure, since
                    # interest accrues without a row on the ledger.
                    account.balance_truth_source = (
                        "hybrid"
                        if account.account_type == AccountType.CREDIT_CARD
                        else "latest_statement"
                    )

                    # Loan PDFs also report the original principal and APR.
                    # Persist them so the account detail page can show the
                    # term structure without the user re-entering it.
                    if loan_rate is not None and account.interest_rate is None:
                        account.interest_rate = loan_rate
                    if (
                        loan_principal is not None
                        and account.original_principal_balance is None
                    ):
                        account.original_principal_balance = _Dec(str(loan_principal))

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
                        if stmt_date is not None else naive_utc_now()
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
                        else naive_utc_now()
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

                # 3) Scheduled payment from the statement, if we have a due
                #    date. Prefer the full statement balance (pay-in-full) as
                #    the planned amount — that's what most people pay and it
                #    gives an accurate forecast — falling back to the minimum.
                #    Both figures are recorded in notes for reference.
                if due_date is not None:
                    # Surface the due date on the account itself.
                    account.payment_due_date = due_date

                if account.account_type == AccountType.CREDIT_CARD:
                    # A card's payment is a transfer from the account that
                    # pays it, kept in step with the latest statement.
                    from app.services.card_payment import sync_card_payment
                    sync_card_payment(db, account)
                elif due_date is not None and (min_pay is not None or new_bal):
                    sym = getattr(account, "currency_symbol", "") or ""
                    full_owed = abs(float(new_bal)) if new_bal else 0.0
                    min_owed = float(min_pay) if min_pay is not None else 0.0
                    if full_owed > 0:
                        plan_amount = _Dec(str(-full_owed))
                        plan_desc = f"{account.name} — Statement Balance"
                    else:
                        plan_amount = _Dec(str(-min_owed))
                        plan_desc = f"{account.name} — Minimum Payment"
                    note_bits = []
                    if full_owed > 0:
                        note_bits.append(f"Pay in full: {sym}{full_owed:,.2f}")
                    if min_owed > 0:
                        note_bits.append(f"Minimum: {sym}{min_owed:,.2f}")
                    plan_notes = " · ".join(note_bits) or None

                    existing_sched = db.execute(
                        _sel(ScheduledPayment).where(
                            ScheduledPayment.account_id == account_id,
                            ScheduledPayment.source == "statement",
                            ScheduledPayment.active.is_(True),
                        ).limit(1)
                    ).scalar_one_or_none()
                    from app.services.scheduled_dismissal import is_dismissed
                    if existing_sched is not None:
                        existing_sched.next_due_date = due_date
                        existing_sched.day_of_month = due_date.day
                        existing_sched.amount = plan_amount
                        existing_sched.description = plan_desc
                        existing_sched.notes = plan_notes
                    elif is_dismissed(db, account_id, plan_desc):
                        # The user deleted this one — don't rebuild it.
                        pass
                    else:
                        db.add(ScheduledPayment(
                            account_id=account_id,
                            user_id=owner_of_account(db, account_id),
                            description=plan_desc,
                            amount=plan_amount,
                            amount_type="estimated",
                            currency=account.currency or "USD",
                            frequency="monthly",
                            next_due_date=due_date,
                            day_of_month=due_date.day,
                            notes=plan_notes,
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
    import urllib.parse as _u
    if stmt_warning:
        url += f"&stmt_warning={_u.quote(stmt_warning)}"
    # Surface every best-effort failure that lost data the user expected
    # (mortgage metadata, overdraft, scheduled matching, etc.) so partial
    # successes are never silent.
    for warning in extraction_warnings:
        url += f"&warning={_u.quote(warning)}"
    return url


@router.post("/ibkr-confirm")
def ibkr_confirm(
    request: Request,
    account_id: int = Form(...),
    filepath: str = Form(...),
    queue: list[str] = Form(default=[]),
    done: list[str] = Form(default=[]),
    prev_mapping: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    from app.services.upload_safety import assert_user_owns_path, UnsafeFilenameError
    try:
        assert_user_owns_path(settings.upload_dir, user.id, filepath)
    except UnsafeFilenameError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    try:
        parsed = parse_ibkr_csv(filepath)
        stats = apply_ibkr_statement(db, account_id, parsed)
    except Exception as exc:
        q, d, prev = _run_from_form(queue, done, prev_mapping)
        return _upload_error(request, db, user, f"IBKR import failed: {exc}", _run_ctx(q, d + [{"name": _display_name(filepath), "status": "failed", "notes": [f"IBKR import failed: {exc}"]}], prev))

    positions = stats["positions_updated"]
    trades = stats["trades_added"]
    dividends = stats["dividends_added"]
    return _finish(request, db, user, f"/portfolio?ibkr_imported=1&positions={positions}&trades={trades}&dividends={dividends}", filepath=filepath, account_id=account_id,
                   queue=queue, done=done, prev_mapping=prev_mapping)


@router.post("/rsu-confirm")
def rsu_confirm(
    request: Request,
    account_id: int = Form(...),
    filepath: str = Form(...),
    queue: list[str] = Form(default=[]),
    done: list[str] = Form(default=[]),
    prev_mapping: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    from app.services.upload_safety import assert_user_owns_path, UnsafeFilenameError
    try:
        assert_user_owns_path(settings.upload_dir, user.id, filepath)
    except UnsafeFilenameError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/import", status_code=303)

    try:
        parsed = parse_merrill_rsu_csv(filepath)
        stats = import_rsu_grants(db, account, parsed)
    except Exception as exc:
        q, d, prev = _run_from_form(queue, done, prev_mapping)
        return _upload_error(request, db, user, f"RSU import failed: {exc}", _run_ctx(q, d + [{"name": _display_name(filepath), "status": "failed", "notes": [f"RSU import failed: {exc}"]}], prev))

    grants = stats["grants_created"] + stats["grants_updated"]
    return _finish(request, db, user, f"/accounts/{account_id}?rsu_imported=1&grants={grants}&vests={stats['vests_created']}", filepath=filepath, account_id=account_id,
                   queue=queue, done=done, prev_mapping=prev_mapping)


@router.post("/epa-confirm")
def epa_confirm(
    request: Request,
    account_id: int = Form(...),
    filepath: str = Form(...),
    queue: list[str] = Form(default=[]),
    done: list[str] = Form(default=[]),
    prev_mapping: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    from app.services.upload_safety import assert_user_owns_path, UnsafeFilenameError
    try:
        assert_user_owns_path(settings.upload_dir, user.id, filepath)
    except UnsafeFilenameError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/import", status_code=303)

    try:
        parsed = parse_epa_pension_pdf(filepath)
        stats = import_pension_positions(db, account, parsed)
    except Exception as exc:
        q, d, prev = _run_from_form(queue, done, prev_mapping)
        return _upload_error(request, db, user, f"ePA pension import failed: {exc}", _run_ctx(q, d + [{"name": _display_name(filepath), "status": "failed", "notes": [f"ePA pension import failed: {exc}"]}], prev))

    return _finish(request, db, user, f"/accounts/{account_id}?epa_imported=1&funds={stats['funds']}", filepath=filepath, account_id=account_id,
                   queue=queue, done=done, prev_mapping=prev_mapping)


@router.post("/revolut-confirm")
def revolut_confirm(
    request: Request,
    account_id: int = Form(...),
    filepath: str = Form(...),
    sections: list[str] = Form(default=[]),
    queue: list[str] = Form(default=[]),
    done: list[str] = Form(default=[]),
    prev_mapping: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    from app.services.upload_safety import assert_user_owns_path, UnsafeFilenameError
    try:
        assert_user_owns_path(settings.upload_dir, user.id, filepath)
    except UnsafeFilenameError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    from decimal import Decimal
    from app.models.transaction import Transaction
    from app.models.import_batch import ImportBatch, ImportSource
    from app.models.account import Account, LIABILITY_TYPES

    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/import", status_code=303)

    include = set(sections) if sections else {"main"}

    stmt_ccy = revolut_statement_currency(filepath)
    if stmt_ccy and (account.currency or "").upper() != stmt_ccy:
        q, d, prev = _run_from_form(queue, done, prev_mapping)
        return _upload_error(request, db, user, f"This is a {stmt_ccy} statement but {account.name} is a {account.currency} account.", _run_ctx(q, d + [{"name": _display_name(filepath), "status": "failed", "notes": [f"This is a {stmt_ccy} statement but {account.name} is a {account.currency} account."]}], prev))

    from app.services.revolut_import import import_revolut_statement
    try:
        batch = import_revolut_statement(
            db, account_id, filepath, include_sections=include,
            account_currency=account.currency or "GBP",
        )
    except Exception as exc:
        q, d, prev = _run_from_form(queue, done, prev_mapping)
        return _upload_error(request, db, user, f"Revolut PDF import failed: {exc}", _run_ctx(q, d + [{"name": _display_name(filepath), "status": "failed", "notes": [f"Revolut PDF import failed: {exc}"]}], prev))

    # Same scheduled-payment matching every other import runs — without it a
    # Revolut statement never settled its direct debits and subscriptions.
    try:
        from app.services.scheduled_matcher import backfill_matches, match_batch
        match_batch(db, batch.id)
        backfill_matches(db, account_id=account_id)
        db.commit()
    except Exception as exc:
        log.warning(
            "scheduled_matcher failed for Revolut batch %s: %s",
            batch.id, exc, exc_info=True,
        )
        db.rollback()

    imported = batch.row_count
    dupes = getattr(batch, "_duplicates_skipped", 0)
    return _finish(request, db, user, f"/accounts/{account_id}?imported={imported}&duplicates={dupes}", filepath=filepath, account_id=account_id,
                   queue=queue, done=done, prev_mapping=prev_mapping)
