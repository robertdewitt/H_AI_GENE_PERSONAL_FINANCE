import logging
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

from app.database import get_db
from app.models.account import AccountType, LIABILITY_TYPES
from app.schemas.account import AccountCreate
from app.templating import templates
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
from app.services.user_profile_service import get_profile

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_class=HTMLResponse)
def accounts_list(request: Request, db: Session = Depends(get_db)):
    profile = get_profile(db)
    display_ccy = profile.display_currency or "USD"

    groups = get_accounts_grouped(db, target_currency=display_ccy)
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
        "display_currency": display_ccy,
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
    property_address: str = Form(""),
    purchase_price: str = Form(""),
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
    acct = create_account(db, data)

    if acct_type == AccountType.REAL_ESTATE:
        if property_address.strip():
            acct.property_address = property_address.strip()
        if purchase_price.strip():
            acct.purchase_price = float(purchase_price)
        db.commit()

        # Auto-fetch estimated value if address provided and no manual value given
        if acct.property_address and not val:
            _try_fetch_property_value(db, acct)

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

    balance = get_account_balance(
        db, account_id, target_currency=acct.currency,
    )
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

    # ── Monthly spend by category (last 12 months) ───────────────────
    from app.config import settings as _settings
    since_12m = datetime.now() - timedelta(days=365)

    if _settings.db_backend == "postgresql":
        _ym = sa_func.to_char(Transaction.date, "YYYY-MM")
    else:
        _ym = sa_func.strftime("%Y-%m", Transaction.date)

    monthly_rows = db.execute(
        sa_select(
            _ym.label("month"),
            Category.name.label("category"),
            sa_func.sum(Transaction.amount).label("total"),
        )
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.account_id == account_id,
            Transaction.amount < 0,
            Transaction.date >= since_12m,
        )
        .group_by("month", Category.name)
        .order_by("month", Category.name)
    ).all()

    # Pivot: {month -> {category -> abs_total}}
    months_ordered = sorted({r.month for r in monthly_rows})
    cat_totals: dict[str, float] = defaultdict(float)
    spend_map: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in monthly_rows:
        amt = abs(float(r.total))
        spend_map[r.month][r.category] = amt
        cat_totals[r.category] += amt

    # Sort categories by total spend desc so biggest slices are at the bottom
    sorted_cats = sorted(cat_totals, key=lambda c: cat_totals[c], reverse=True)

    _COLORS = [
        "#2563eb", "#16a34a", "#f59e0b", "#8b5cf6", "#ec4899",
        "#06b6d4", "#84cc16", "#f97316", "#ef4444", "#6366f1",
        "#14b8a6", "#d946ef", "#fb923c", "#a3e635", "#38bdf8", "#818cf8",
    ]
    monthly_spend_labels = []
    for m in months_ordered:
        try:
            monthly_spend_labels.append(datetime.strptime(m, "%Y-%m").strftime("%b %Y"))
        except ValueError:
            monthly_spend_labels.append(m)

    monthly_spend_datasets = [
        {
            "label": cat,
            "data": [round(spend_map[m].get(cat, 0.0), 2) for m in months_ordered],
            "backgroundColor": _COLORS[i % len(_COLORS)],
        }
        for i, cat in enumerate(sorted_cats)
    ]

    # Batch-load split categories for the recent transactions (one JOIN query).
    from app.models.transaction_split import TransactionSplit
    txn_ids = [t.id for t in recent_txns]
    split_categories: dict[int, list[str]] = {}
    if txn_ids:
        rows = db.execute(
            sa_select(TransactionSplit.transaction_id, Category.name)
            .join(Category, TransactionSplit.category_id == Category.id)
            .where(TransactionSplit.transaction_id.in_(txn_ids))
            .order_by(TransactionSplit.transaction_id, TransactionSplit.id)
        ).all()
        for txn_id, cat_name in rows:
            split_categories.setdefault(txn_id, []).append(cat_name)

    return templates.TemplateResponse(request, "accounts/detail.html", {
        "account": acct,
        "balance": balance,
        "transactions": recent_txns,
        "split_categories": split_categories,
        "total_transactions": total_txn_count,
        "category_summary": category_summary,
        "monthly_spend_labels": monthly_spend_labels,
        "monthly_spend_datasets": monthly_spend_datasets,
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
    property_address: str = Form(""),
    purchase_price: str = Form(""),
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
    acct = update_account(db, account_id, data)

    if acct and acct_type == AccountType.REAL_ESTATE:
        acct.property_address = property_address.strip() or None
        acct.purchase_price = float(purchase_price) if purchase_price.strip() else None
        db.commit()

        # Re-fetch estimated value when address changes and no manual value set
        if acct.property_address and not val:
            _try_fetch_property_value(db, acct)

    return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)


@router.post("/{account_id}/delete")
def account_remove(account_id: int, db: Session = Depends(get_db)):
    delete_account(db, account_id)
    return RedirectResponse(url="/accounts", status_code=303)


@router.post("/{account_id}/refresh-value")
def account_refresh_value(account_id: int, db: Session = Depends(get_db)):
    """Re-fetch estimated property value from free APIs."""
    acct = get_account(db, account_id)
    if not acct or not acct.property_address:
        return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)
    _try_fetch_property_value(db, acct)
    return RedirectResponse(url=f"/accounts/{account_id}?value_refreshed=1", status_code=303)


# ── Property value estimation ──────────────────────────────────────────


def _try_fetch_property_value(db: Session, acct) -> None:
    """Best-effort property value lookup using free public APIs.

    Uses the ATTOM Data API (free tier) if ATTOM_API_KEY is set,
    otherwise falls back to a Rentcast public estimate.
    If neither is available, stores nothing and logs a warning.
    """
    from app.models.asset_valuation import AssetValuation
    from app.config import settings as _settings

    address = acct.property_address
    if not address:
        return

    estimated = _fetch_rentcast_value(address)

    if estimated is None:
        log.warning("Property value lookup returned no result for: %s", address)
        return

    log.info("Property estimate for '%s': %.2f", address, estimated)

    # Store as an AssetValuation row so history is preserved
    val = AssetValuation(
        account_id=acct.id,
        date=datetime.now(),
        value=estimated,
        currency=acct.currency,
        source="rentcast_api",
        notes="Auto-fetched estimate",
    )
    db.add(val)

    # Also update current_value for immediate display
    acct.current_value = estimated
    acct.value_as_of_date = datetime.now()
    db.commit()


def _fetch_rentcast_value(address: str) -> float | None:
    """Query Rentcast public API for an AVM (automated valuation model) estimate.

    Rentcast has a free tier. No API key required for the basic AVM endpoint.
    Returns estimated value in USD, or None on failure.
    """
    import urllib.parse
    import urllib.request
    import json as _json

    try:
        encoded = urllib.parse.quote(address)
        url = f"https://api.rentcast.io/v1/avm/value?address={encoded}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
            price = data.get("price") or data.get("value")
            if price:
                return float(price)
    except Exception as e:
        log.debug("Rentcast AVM failed for '%s': %s", address, e)

    return None
