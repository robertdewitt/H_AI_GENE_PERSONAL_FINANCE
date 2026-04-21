import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
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
from app.services.property_valuation import estimate_property_value, provider_status as prop_provider_status

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
def account_new_form(request: Request, db: Session = Depends(get_db)):
    mortgage_accounts = [
        a for a in list_accounts(db)
        if a.account_type == AccountType.MORTGAGE
    ]
    return templates.TemplateResponse(request, "accounts/form.html", {
        "account": None,
        "account_types": list(AccountType),
        "liability_types": [t.value for t in LIABILITY_TYPES],
        "mortgage_accounts": mortgage_accounts,
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
    purchase_date: str = Form(""),
    linked_mortgage_account_id: str = Form(""),
    db: Session = Depends(get_db),
):
    acct_type = AccountType(account_type)
    is_asset = acct_type not in LIABILITY_TYPES
    val = Decimal(current_value) if current_value.strip() else None

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

    _PHYSICAL_ASSET_TYPES = {AccountType.REAL_ESTATE, AccountType.VEHICLE, AccountType.COLLECTIBLE}
    if acct_type in _PHYSICAL_ASSET_TYPES:
        if purchase_price.strip():
            acct.purchase_price = float(purchase_price)
        if purchase_date.strip():
            try:
                acct.purchase_date = datetime.strptime(purchase_date.strip(), "%Y-%m-%d")
            except ValueError:
                pass

    if acct_type == AccountType.REAL_ESTATE:
        if property_address.strip():
            acct.property_address = property_address.strip()
        if linked_mortgage_account_id.strip():
            try:
                acct.linked_mortgage_account_id = int(linked_mortgage_account_id)
            except ValueError:
                pass
        # Real estate balance comes from manual mark, not transactions
        acct.balance_truth_source = "manual_mark"
        if val:
            acct.current_value = val
            acct.value_as_of_date = datetime.now()
        elif acct.purchase_price and not acct.current_value:
            # Fall back to purchase price until a market value is fetched
            acct.current_value = acct.purchase_price
            acct.value_as_of_date = datetime.now()
        db.commit()

        # Auto-fetch estimated value if address provided and no manual value given
        if acct.property_address and not val:
            _try_fetch_property_value(db, acct)
    elif acct_type in _PHYSICAL_ASSET_TYPES:
        acct.balance_truth_source = "manual_mark"
        if val:
            acct.current_value = val
            acct.value_as_of_date = datetime.now()
        elif acct.purchase_price and not acct.current_value:
            acct.current_value = acct.purchase_price
            acct.value_as_of_date = datetime.now()
        db.commit()

    return RedirectResponse(url="/accounts", status_code=303)


@router.get("/{account_id}", response_class=HTMLResponse)
def account_detail(
    request: Request,
    account_id: int,
    background_tasks: BackgroundTasks,
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
    cat_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    spend_map: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0.00"))
    )
    for r in monthly_rows:
        amt = abs(r.total or Decimal("0.00"))
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
            "data": [round(spend_map[m].get(cat, Decimal("0.00")), 2) for m in months_ordered],
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

    # For real estate accounts, determine which provider will be used
    prop_status = None
    mortgage_balance = None
    mortgage_account = None
    if acct.account_type.value == "real_estate":
        profile = get_profile(db)
        prop_status = prop_provider_status(
            currency=acct.currency,
            country_of_residence=profile.country_of_residence,
            rentcast_api_key=profile.rentcast_api_key,
            property_data_api_key=profile.property_data_api_key,
            domain_api_key=profile.domain_api_key,
        )
        if acct.linked_mortgage_account_id:
            mortgage_account = get_account(db, acct.linked_mortgage_account_id)
            if mortgage_account:
                mortgage_balance = get_account_balance(
                    db, acct.linked_mortgage_account_id, target_currency=acct.currency,
                )

    # Auto-refresh property value in the background if stale or never fetched
    value_refreshing = False
    if (
        acct.account_type.value == "real_estate"
        and acct.property_address
        and (
            acct.value_as_of_date is None
            or (datetime.now() - acct.value_as_of_date).days >= _VALUATION_STALE_DAYS
        )
    ):
        background_tasks.add_task(_background_refresh_property_value, account_id)
        value_refreshing = True

    return templates.TemplateResponse(request, "accounts/detail.html", {
        "account": acct,
        "balance": balance,
        "transactions": recent_txns,
        "split_categories": split_categories,
        "total_transactions": total_txn_count,
        "category_summary": category_summary,
        "monthly_spend_labels": monthly_spend_labels,
        "monthly_spend_datasets": monthly_spend_datasets,
        "prop_status": prop_status,
        "mortgage_account": mortgage_account,
        "mortgage_balance": mortgage_balance,
        "now": datetime.now(),
        "value_refreshing": value_refreshing,
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

    mortgage_accounts = [
        a for a in list_accounts(db)
        if a.account_type == AccountType.MORTGAGE and a.id != account_id
    ]
    return templates.TemplateResponse(request, "accounts/form.html", {
        "account": acct,
        "account_types": list(AccountType),
        "liability_types": [t.value for t in LIABILITY_TYPES],
        "mortgage_accounts": mortgage_accounts,
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
    purchase_date: str = Form(""),
    linked_mortgage_account_id: str = Form(""),
    db: Session = Depends(get_db),
):
    acct_type = AccountType(account_type)
    is_asset = acct_type not in LIABILITY_TYPES
    val = Decimal(current_value) if current_value.strip() else None

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
    if not acct:
        return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)

    _PHYSICAL_ASSET_TYPES = {AccountType.REAL_ESTATE, AccountType.VEHICLE, AccountType.COLLECTIBLE}
    if acct_type in _PHYSICAL_ASSET_TYPES:
        acct.purchase_price = float(purchase_price) if purchase_price.strip() else None
        if purchase_date.strip():
            try:
                acct.purchase_date = datetime.strptime(purchase_date.strip(), "%Y-%m-%d")
            except ValueError:
                pass
        else:
            acct.purchase_date = None

    if acct_type == AccountType.REAL_ESTATE:
        acct.property_address = property_address.strip() or None
        acct.linked_mortgage_account_id = int(linked_mortgage_account_id) if linked_mortgage_account_id.strip() else None
        acct.balance_truth_source = "manual_mark"
        if val:
            acct.current_value = val
            acct.value_as_of_date = datetime.now()
        elif acct.purchase_price and not acct.current_value:
            acct.current_value = acct.purchase_price
            acct.value_as_of_date = datetime.now()
        db.commit()

        # Re-fetch estimated value when address changes and no manual value set
        if acct.property_address and not val:
            _try_fetch_property_value(db, acct)
    elif acct_type in _PHYSICAL_ASSET_TYPES:
        acct.balance_truth_source = "manual_mark"
        if val:
            acct.current_value = val
            acct.value_as_of_date = datetime.now()
        elif acct.purchase_price and not acct.current_value:
            acct.current_value = acct.purchase_price
            acct.value_as_of_date = datetime.now()
        db.commit()

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

    from app.models.asset_valuation import AssetValuation as _AV
    before_count = db.query(_AV).filter(_AV.account_id == account_id).count()
    _try_fetch_property_value(db, acct)
    after_count = db.query(_AV).filter(_AV.account_id == account_id).count()

    if after_count > before_count:
        return RedirectResponse(url=f"/accounts/{account_id}?value_refreshed=1", status_code=303)
    else:
        return RedirectResponse(url=f"/accounts/{account_id}?value_refresh_failed=1", status_code=303)


# ── Property value estimation ──────────────────────────────────────────


def _background_refresh_property_value(account_id: int) -> None:
    """Run a property valuation refresh in a background task (own DB session)."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        acct = get_account(db, account_id)
        if acct:
            _try_fetch_property_value(db, acct)
    except Exception as e:
        log.warning("Background property refresh failed for account %s: %s", account_id, e)
    finally:
        db.close()


_VALUATION_STALE_DAYS = 1    # refresh if value is older than this (daily)


def _try_fetch_property_value(db: Session, acct) -> None:
    """Fetch an estimated property value using the appropriate regional provider.

    Routes to Rentcast (US), HM Land Registry / PropertyData (UK),
    or Domain API (AU) based on account currency and user profile.
    Stores the result as an AssetValuation row and updates current_value.
    """
    from app.models.asset_valuation import AssetValuation

    if not acct.property_address:
        return

    profile = get_profile(db)
    result = estimate_property_value(
        address=acct.property_address,
        currency=acct.currency,
        country_of_residence=profile.country_of_residence,
        rentcast_api_key=profile.rentcast_api_key,
        property_data_api_key=profile.property_data_api_key,
        domain_api_key=profile.domain_api_key,
    )

    if result is None:
        log.warning("Property value lookup returned no result for: %s", acct.property_address)
        return

    log.info(
        "Property estimate for '%s': %.2f (source: %s, is_estimate: %s)",
        acct.property_address, result.value, result.source, result.is_estimate,
    )

    val = AssetValuation(
        account_id=acct.id,
        date=datetime.now(),
        value=result.value,
        currency=acct.currency,
        source=result.source,
        notes=result.notes or result.source_label,
    )
    db.add(val)
    acct.current_value = result.value
    acct.value_as_of_date = datetime.now()
    acct.balance_truth_source = "manual_mark"
    db.commit()
