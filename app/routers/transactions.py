import json
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates
from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.models.transfer_link import TransferLink
from app.services.split_service import list_splits, replace_transaction_splits
from app.services.transaction_truth import apply_truth_after_transaction_update
from app.services.categorizer import (
    categorize_batch,
    learn_from_correction,
    suggest_categories,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])


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
        from sqlalchemy import exists
        clauses.append(Transaction.category_id.is_(None))
        # Exclude transactions that have at least one split with a category assigned
        clauses.append(
            ~exists().where(
                TransactionSplit.transaction_id == Transaction.id,
                TransactionSplit.category_id.is_not(None),
            )
        )
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


def _safe_int(val: str | None) -> int | None:
    if not val or not val.strip():
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_decimal(val: str | None) -> Decimal | None:
    if not val or not val.strip():
        return None
    try:
        return Decimal(val)
    except (ValueError, TypeError):
        return None


@router.get("", response_class=HTMLResponse)
def transactions_list(
    request: Request,
    account_id: str | None = Query(None),
    category_id: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    search: str | None = Query(None),
    is_transfer: str | None = Query(None),
    amount_min: str | None = Query(None),
    amount_max: str | None = Query(None),
    currency: str | None = Query(None),
    uncategorized: bool | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    account_id_val = _safe_int(account_id)
    category_id_val = _safe_int(category_id)
    amount_min_val = _safe_decimal(amount_min)
    amount_max_val = _safe_decimal(amount_max)

    transfer_flag = None
    if is_transfer == "true":
        transfer_flag = True
    elif is_transfer == "false":
        transfer_flag = False

    clauses = _build_filters(
        account_id_val, category_id_val, date_from, date_to, search,
        transfer_flag, amount_min_val, amount_max_val, currency,
        uncategorized,
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

    currencies = db.execute(
        select(Transaction.original_currency)
        .distinct()
        .order_by(Transaction.original_currency)
    ).scalars().all()

    total_pages = max(1, (total_count + per_page - 1) // per_page)

    # Flash message after auto-categorize apply
    auto_cat_applied = request.query_params.get("auto_cat_applied")

    # Batch-load split categories for all transactions on this page.
    # One query instead of N — used to display "Wine · Gifts · Shopping" in the list.
    txn_ids = [t.id for t in txns]
    split_categories: dict[int, list[str]] = {}
    if txn_ids:
        from app.models.transaction_split import TransactionSplit
        rows = db.execute(
            select(
                TransactionSplit.transaction_id,
                Category.name,
            )
            .join(Category, TransactionSplit.category_id == Category.id)
            .where(TransactionSplit.transaction_id.in_(txn_ids))
            .order_by(TransactionSplit.transaction_id, TransactionSplit.id)
        ).all()
        for txn_id, cat_name in rows:
            split_categories.setdefault(txn_id, []).append(cat_name)

    return templates.TemplateResponse(request, "transactions/list.html", {
        "transactions": txns,
        "split_categories": split_categories,
        "accounts": accounts,
        "categories": categories,
        "currencies": currencies,
        "auto_cat_applied": int(auto_cat_applied) if auto_cat_applied and auto_cat_applied.isdigit() else None,
        "filters": {
            "account_id": account_id_val,
            "category_id": category_id_val,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "search": search or "",
            "is_transfer": is_transfer or "",
            "amount_min": amount_min_val if amount_min_val is not None else "",
            "amount_max": amount_max_val if amount_max_val is not None else "",
            "currency": currency or "",
            "uncategorized": uncategorized,
        },
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
    })


# ── Single transaction edit ──────────────────────────────────────────

@router.get("/{txn_id:int}/edit", response_class=HTMLResponse)
def transaction_edit_form(
    request: Request,
    txn_id: int,
    return_url: str | None = Query(None),
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

    # Splits serialised as category + amount only — spend metadata is system-derived
    splits_rows = [
        {
            "amount": s.amount_native,
            "currency": s.currency,
            "category_id": s.category_id,
            "notes": s.notes or "",
        }
        for s in list_splits(db, txn.id)
    ]
    splits_json = json.dumps(splits_rows, default=lambda o: float(o) if isinstance(o, Decimal) else str(o))
    categories_json = [
        {"id": c.id, "name": c.name, "type": c.category_type.value}
        for c in categories
    ]

    return templates.TemplateResponse(request, "transactions/edit.html", {
        "txn": txn,
        "categories": categories,
        "categories_json": categories_json,
        "accounts": accounts,
        "return_url": return_url or f"/accounts/{txn.account_id}",
        "splits_json": splits_json,
    })


@router.post("/{txn_id:int}/edit")
def transaction_update(
    request: Request,
    txn_id: int,
    date: str = Form(...),
    description: str = Form(...),
    amount: Decimal = Form(...),
    category_id: str = Form(""),
    is_transfer: bool = Form(False),
    transfer_account_id: str = Form(""),
    splits_json: str = Form(""),
    return_url: str = Form(""),
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
    categories_json = [
        {"id": c.id, "name": c.name, "type": c.category_type.value}
        for c in categories
    ]

    def _render_form(split_error=None, status_code=200):
        return templates.TemplateResponse(request, "transactions/edit.html", {
            "txn": txn,
            "categories": categories,
            "categories_json": categories_json,
            "accounts": accounts,
            "return_url": return_url or f"/accounts/{txn.account_id}",
            "splits_json": splits_json,
            "split_error": split_error,
        }, status_code=status_code)

    split_error = None
    lines = None
    if splits_json.strip():
        try:
            lines = json.loads(splits_json)
        except json.JSONDecodeError:
            split_error = "Invalid splits JSON."
        if split_error is None and lines is not None:
            if not isinstance(lines, list):
                split_error = "Splits must be a JSON array."
            else:
                for row in lines:
                    if "amount" not in row:
                        split_error = "Each split needs an amount."
                        break
                if split_error is None:
                    try:
                        total = sum(Decimal(str(row["amount"])) for row in lines)
                    except (TypeError, KeyError):
                        split_error = "Invalid amount in splits."
                    else:
                        if abs(total - amount) > Decimal("0.02"):
                            split_error = (
                                f"Splits sum to {total:.2f} but transaction amount is {amount:.2f}."
                            )

    if split_error:
        return _render_form(split_error=split_error, status_code=400)

    old_event_type = txn.event_type
    old_category_id = txn.category_id
    txn.date = datetime.strptime(date, "%Y-%m-%d")
    txn.description = description
    txn.amount = amount
    txn.is_transfer = is_transfer

    new_cat_id = int(category_id) if category_id.strip() else None
    txn.category_id = new_cat_id

    if new_cat_id and new_cat_id != old_category_id:
        learn_from_correction(db, description, new_cat_id)

    if splits_json.strip() and lines is not None:
        val = replace_transaction_splits(db, txn_id, lines)
        if not val.valid:
            return _render_form(split_error="; ".join(val.warnings), status_code=400)

    if is_transfer and transfer_account_id.strip():
        dest_account_id = int(transfer_account_id)
        if dest_account_id != txn.account_id:
            _link_transfer(db, txn, dest_account_id)
    elif not is_transfer and txn.transfer_link_id:
        txn.transfer_link_id = None

    apply_truth_after_transaction_update(db, txn, old_event_type)

    db.commit()

    redirect_to = return_url.strip() if return_url else f"/accounts/{txn.account_id}"
    return RedirectResponse(url=redirect_to, status_code=303)


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

@router.post("/{txn_id:int}/delete")
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
    return_url: str = Form("/transactions"),
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
    return RedirectResponse(url=return_url, status_code=303)


@router.post("/bulk/delete")
def bulk_delete(
    txn_ids: str = Form(...),
    return_url: str = Form("/transactions"),
    db: Session = Depends(get_db),
):
    ids = [int(x) for x in txn_ids.split(",") if x.strip().isdigit()]
    db.execute(
        Transaction.__table__.delete().where(Transaction.id.in_(ids))
    )
    db.commit()
    return RedirectResponse(url=return_url, status_code=303)


@router.post("/bulk/toggle-transfer")
def bulk_toggle_transfer(
    txn_ids: str = Form(...),
    is_transfer: bool = Form(True),
    return_url: str = Form("/transactions"),
    db: Session = Depends(get_db),
):
    ids = [int(x) for x in txn_ids.split(",") if x.strip().isdigit()]
    txns = db.execute(
        select(Transaction).where(Transaction.id.in_(ids))
    ).scalars().all()
    for txn in txns:
        txn.is_transfer = is_transfer
    db.commit()
    return RedirectResponse(url=return_url, status_code=303)


# ── Auto-categorize ─────────────────────────────────────────────────

@router.get("/auto-categorize/preview", response_class=HTMLResponse)
def auto_categorize_preview(
    request: Request,
    limit: int = Query(200),
    db: Session = Depends(get_db),
):
    suggestions = suggest_categories(db, limit=limit)
    categories = db.execute(select(Category).order_by(Category.name)).scalars().all()
    return templates.TemplateResponse(request, "transactions/auto_categorize_preview.html", {
        "suggestions": suggestions,
        "categories": categories,
        "limit": limit,
    })


@router.post("/auto-categorize/apply")
def auto_categorize_apply(
    request: Request,
    assignments: str = Form(...),   # JSON: [[txn_id, category_id], ...]
    db: Session = Depends(get_db),
):
    try:
        pairs = json.loads(assignments)
    except json.JSONDecodeError:
        return RedirectResponse(url="/transactions", status_code=303)

    applied = 0
    for txn_id, category_id in pairs:
        txn = db.get(Transaction, int(txn_id))
        if txn and category_id:
            old_cat = txn.category_id
            txn.category_id = int(category_id)
            if txn.category_id != old_cat:
                learn_from_correction(db, txn.description, txn.category_id)
            applied += 1

    db.commit()
    return RedirectResponse(url=f"/transactions?auto_cat_applied={applied}", status_code=303)


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
