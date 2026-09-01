import logging
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from app.services.clock import naive_utc_now
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    get_account_balance_rich,
    get_accounts_grouped,
    get_transaction_count,
    list_accounts,
    close_account,
    next_payment_due,
    next_payment_due_map,
    reopen_account,
    split_closed_accounts,
    update_account,
)
from app.services.user_profile_service import get_profile
from app.services.property_valuation import estimate_property_value, provider_status as prop_provider_status

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/address-search")
def address_search(q: str = Query(..., min_length=3)):
    """Server-side proxy to Nominatim so the browser isn't blocked by CORS/UA rules."""
    import json as _json
    try:
        url = (
            "https://nominatim.openstreetmap.org/search"
            f"?format=json&addressdetails=0&limit=6&q={urllib.parse.quote(q)}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "FinancialHygieneApp/1.0 (personal finance tool)",
                "Accept-Language": "en",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
        return JSONResponse([{"display_name": item["display_name"]} for item in data])
    except Exception as e:
        log.debug("Address search failed: %s", e)
        return JSONResponse([])


_SPEND_PRESETS = [
    ("1y",  "1 Year"),
    ("ytd", "YTD"),
    ("2y",  "2 Years"),
    ("mtd", "MTD"),
    ("all", "All"),
]

# Accounts whose activity is held in the brokerage tables (trades, dividends,
# position lots) rather than as generic Transaction rows.
_MARKET_ACCOUNT_TYPES = {
    AccountType.BROKERAGE,
    AccountType.IRA,
    AccountType.ROTH_IRA,
    AccountType.FOUR_OH_ONE_K,
}


def _preset_to_since(preset: str | None) -> datetime:
    from datetime import date
    today = naive_utc_now()
    if preset == "ytd":
        return datetime(today.year, 1, 1)
    if preset == "mtd":
        return datetime(today.year, today.month, 1)
    if preset == "2y":
        return today - timedelta(days=730)
    if preset == "all":
        return datetime(2000, 1, 1)
    return today - timedelta(days=365)   # default: 1y


@router.get("", response_class=HTMLResponse)
def accounts_list(
    request: Request,
    preset: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from sqlalchemy import func as sa_func, select as sa_select
    from app.models.transaction import Transaction
    from app.models.category import Category, CategoryType as _CatType
    from app.config import settings as _settings

    if preset is None:
        preset = "1y"

    profile = get_profile(db)
    display_ccy = profile.display_currency or "USD"

    groups = get_accounts_grouped(db, target_currency=display_ccy)
    _all_accts = [item["account"] for items in groups.values() for item in items]
    due_dates = next_payment_due_map(db, _all_accts)
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

    # Closed accounts still count toward the totals above — closing is
    # organisational only. Split them out so the active list stays uncluttered.
    groups, closed_accounts = split_closed_accounts(groups)

    since = _preset_to_since(preset)

    if _settings.db_backend == "postgresql":
        _ym = sa_func.to_char(Transaction.date, "YYYY-MM")
    else:
        _ym = sa_func.strftime("%Y-%m", Transaction.date)

    # Expenses: negative non-transfer transactions, grouped by month + category.
    # Special-case: mortgage / loan payments often land categorised as
    # "Account Transfer" (because they move money between owned entities)
    # but they're cash that left the household for the period — include
    # them if the description hits a mortgage/loan keyword so the totals
    # match the user's real outflow.
    from sqlalchemy import and_ as _and, or_ as _or
    _LOAN_DESC_KEYWORDS = (
        "mortgage", "mortgagemain", "mtg", "blackhorse", "black horse",
    )
    _loan_desc_filter = _or(*(
        sa_func.lower(Transaction.description).like(f"%{kw}%")
        for kw in _LOAN_DESC_KEYWORDS
    ))
    # Spending with no category still left the household, so it belongs in the
    # breakdown. An inner join would drop it silently and understate the
    # totals — outer-join it and bucket it under "Uncategorized" instead.
    _cat_label = sa_func.coalesce(Category.name, "Uncategorized")
    _not_transfer_cat = _or(
        Category.id.is_(None),
        Category.category_type != _CatType.TRANSFER,
    )
    # Statement anchors ("Balance brought/carried forward" on a mortgage
    # statement) are balance markers, not money spent. They dwarf real
    # spending if left in — one pair alone is nearly £900k.
    _BALANCE_MARKERS = ("%brought forward%", "%carried forward%", "%opening balance%")
    _not_balance_marker = _and(*(
        sa_func.lower(Transaction.description).notlike(m) for m in _BALANCE_MARKERS
    ))
    monthly_rows = db.execute(
        sa_select(
            _ym.label("month"),
            _cat_label.label("category"),
            sa_func.sum(Transaction.amount).label("total"),
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.amount < 0,
            # Never show transfers (any TRANSFER-type category, e.g.
            # "Account Transfer") as expense spending — money moved between
            # your own accounts is not consumption.
            _not_transfer_cat,
            _not_balance_marker,
            _or(
                Transaction.is_transfer.is_(False),
                # Money to a mortgage/loan still counts as an expense outflow
                # even when flagged is_transfer — as long as it isn't filed
                # under a transfer category (excluded above).
                _loan_desc_filter,
            ),
            Transaction.date >= since,
        )
        .group_by("month", _cat_label)
        .order_by("month", _cat_label)
    ).all()

    # Income: positive non-transfer transactions on *asset* accounts only.
    # Excluding liability accounts (credit cards, loans) keeps a refund
    # or balance-transfer credit out of the income totals — those aren't
    # money the household actually earned.
    from app.models.account import Account as _Acct
    income_rows = db.execute(
        sa_select(
            _ym.label("month"),
            _cat_label.label("category"),
            sa_func.sum(Transaction.amount).label("total"),
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .join(_Acct, _Acct.id == Transaction.account_id)
        .where(
            Transaction.amount > 0,
            Transaction.is_transfer.is_(False),
            _Acct.is_asset.is_(True),
            _not_transfer_cat,
            Transaction.date >= since,
        )
        .group_by("month", _cat_label)
        .order_by("month", _cat_label)
    ).all()

    months_ordered = sorted(
        {r.month for r in monthly_rows} | {r.month for r in income_rows}
    )

    def _build_series(rows):
        cat_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        per_month: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: Decimal("0.00"))
        )
        for r in rows:
            amt = abs(r.total or Decimal("0.00"))
            per_month[r.month][r.category] = amt
            cat_totals[r.category] += amt
        return per_month, cat_totals

    spend_map, spend_totals  = _build_series(monthly_rows)
    income_map, income_totals = _build_series(income_rows)
    sorted_spend_cats  = sorted(spend_totals,  key=lambda c: spend_totals[c],  reverse=True)
    sorted_income_cats = sorted(income_totals, key=lambda c: income_totals[c], reverse=True)

    _COLORS = [
        "#2563eb", "#16a34a", "#f59e0b", "#8b5cf6", "#ec4899",
        "#06b6d4", "#84cc16", "#f97316", "#ef4444", "#6366f1",
        "#14b8a6", "#d946ef", "#fb923c", "#a3e635", "#38bdf8", "#818cf8",
    ]
    spend_labels: list[str] = []
    for m in months_ordered:
        try:
            spend_labels.append(datetime.strptime(m, "%Y-%m").strftime("%b %Y"))
        except ValueError:
            spend_labels.append(m)
    # Income shares the same X axis so the bars line up month-for-month.
    income_labels = spend_labels

    def _datasets(per_month, sorted_cats):
        return [
            {
                "label": cat,
                "data": [
                    round(float(per_month[m].get(cat, Decimal("0.00"))), 2)
                    for m in months_ordered
                ],
                "backgroundColor": _COLORS[i % len(_COLORS)],
            }
            for i, cat in enumerate(sorted_cats)
        ]

    spend_datasets  = _datasets(spend_map,  sorted_spend_cats)
    income_datasets = _datasets(income_map, sorted_income_cats)

    # Classify expense categories as essential (non-discretionary) vs
    # discretionary. Heuristic for now — applied to the category name.
    # If the user wants a different split they can rename the category or
    # we can later add an explicit Category.is_essential flag.
    _ESSENTIAL_KEYWORDS = (
        "rent", "mortgage", "loan", "council tax", "tax",
        "insurance", "utility", "utilities", "electric", "gas bill",
        "water", "sewage", "healthcare", "medical", "doctor", "dentist",
        "prescription", "school", "education", "childcare", "daycare",
        "internet", "phone bill", "broadband", "fuel", "petrol",
        "transit", "transport", "groceries",
    )

    # Explicit per-category overrides set on the Categories page.
    #   True/False win; None falls back to the keyword heuristic.
    _essential_overrides = {
        name: ess
        for name, ess in db.execute(
            sa_select(Category.name, Category.is_essential)
        ).all()
    }

    def _is_essential(category_name: str | None) -> bool:
        if not category_name:
            return False
        override = _essential_overrides.get(category_name)
        if override is not None:
            return override
        cl = category_name.lower()
        return any(kw in cl for kw in _ESSENTIAL_KEYWORDS)

    # Per-category period totals for the summary tables under each chart.
    def _category_breakdown(totals, sorted_cats, mark_essential=False):
        grand = sum(totals.values(), Decimal("0.00"))
        rows = [
            {
                "category": cat,
                "total": round(totals[cat], 2),
                "pct": (
                    round(float(totals[cat] / grand * 100), 1)
                    if grand else 0.0
                ),
                "essential": _is_essential(cat) if mark_essential else None,
            }
            for cat in sorted_cats
        ]
        return rows, round(grand, 2)

    spend_breakdown,  spend_total_period  = _category_breakdown(
        spend_totals,  sorted_spend_cats, mark_essential=True,
    )
    income_breakdown, income_total_period = _category_breakdown(
        income_totals, sorted_income_cats,
    )

    # Essential vs discretionary subtotals for the expense side.
    essential_total = sum(
        (row["total"] for row in spend_breakdown if row["essential"]),
        Decimal("0.00"),
    )
    discretionary_total = sum(
        (row["total"] for row in spend_breakdown if not row["essential"]),
        Decimal("0.00"),
    )

    return templates.TemplateResponse(request, "accounts/list.html", {
        "groups": groups,
        "closed_accounts": closed_accounts,
        "due_dates": due_dates,
        "today": naive_utc_now().date(),
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities,
        "display_currency": display_ccy,
        "preset": preset,
        "presets": _SPEND_PRESETS,
        "spend_labels":   spend_labels,
        "spend_datasets": spend_datasets,
        "income_labels":   income_labels,
        "income_datasets": income_datasets,
        "spend_breakdown":      spend_breakdown,
        "income_breakdown":     income_breakdown,
        "spend_total_period":   spend_total_period,
        "income_total_period":  income_total_period,
        "essential_total":      round(essential_total, 2),
        "discretionary_total":  round(discretionary_total, 2),
        "net_period": income_total_period - spend_total_period,
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
    interest_rate: str = Form(""),
    monthly_payment: str = Form(""),
    statement_balance: str = Form(""),
    statement_balance_as_of: str = Form(""),
    overdraft_limit: str = Form(""),
    overdraft_as_of: str = Form(""),
    payment_due_date: str = Form(""),
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
        value_as_of_date=naive_utc_now() if val is not None else None,
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
            acct.value_as_of_date = naive_utc_now()
        elif acct.purchase_price and not acct.current_value:
            # Fall back to purchase price until a market value is fetched
            acct.current_value = acct.purchase_price
            acct.value_as_of_date = naive_utc_now()
        db.commit()

        # Auto-fetch estimated value if address provided and no manual value given
        if acct.property_address and not val:
            _try_fetch_property_value(db, acct)
    elif acct_type in _PHYSICAL_ASSET_TYPES:
        acct.balance_truth_source = "manual_mark"
        if val:
            acct.current_value = val
            acct.value_as_of_date = naive_utc_now()
        elif acct.purchase_price and not acct.current_value:
            acct.current_value = acct.purchase_price
            acct.value_as_of_date = naive_utc_now()
        db.commit()

    if acct_type == AccountType.MORTGAGE:
        if interest_rate.strip():
            try:
                acct.interest_rate = float(interest_rate) / 100.0
            except ValueError:
                pass
        if monthly_payment.strip():
            try:
                acct.monthly_payment = Decimal(monthly_payment)
            except (ValueError, InvalidOperation) as exc:
                log.warning(
                    "account_form: monthly_payment parse failed (%r): %s",
                    monthly_payment, exc,
                )
        db.commit()

    # Interest rate for non-mortgage interest-bearing accounts (loans,
    # credit cards, personal loans filed under "other").
    _INTEREST_TYPES = {AccountType.CREDIT_CARD, AccountType.LOAN, AccountType.OTHER}
    if acct_type in _INTEREST_TYPES and interest_rate.strip():
        try:
            acct.interest_rate = float(interest_rate) / 100.0
            db.commit()
        except ValueError:
            pass

    stmt_err: str | None = None
    if statement_balance.strip():
        try:
            acct.statement_balance = Decimal(statement_balance)
            acct.statement_balance_as_of = (
                datetime.strptime(statement_balance_as_of.strip(), "%Y-%m-%d")
                if statement_balance_as_of.strip()
                else naive_utc_now()
            )
            if acct.balance_truth_source in (None, "transaction_sum"):
                acct.balance_truth_source = "hybrid"
            db.commit()
        except (ValueError, InvalidOperation) as exc:
            db.rollback()
            stmt_err = f"Could not parse statement balance: {exc}"
            log.warning("account_form: statement_balance parse failed: %s", exc)

    if overdraft_limit.strip():
        try:
            acct.overdraft_limit = Decimal(overdraft_limit)
            acct.overdraft_as_of = (
                datetime.strptime(overdraft_as_of.strip(), "%Y-%m-%d")
                if overdraft_as_of.strip() else naive_utc_now()
            )
            db.commit()
        except (ValueError, InvalidOperation):
            db.rollback()

    if acct_type in LIABILITY_TYPES and payment_due_date.strip():
        try:
            acct.payment_due_date = datetime.strptime(
                payment_due_date.strip(), "%Y-%m-%d"
            ).date()
            db.commit()
        except ValueError:
            db.rollback()

    redirect = "/accounts"
    if stmt_err:
        redirect += "?error=" + urllib.parse.quote(stmt_err)
    return RedirectResponse(url=redirect, status_code=303)


@router.get("/{account_id}", response_class=HTMLResponse)
def account_detail(
    request: Request,
    account_id: int,
    forecast_months: int = 6,
    db: Session = Depends(get_db),
):
    acct = get_account(db, account_id)
    if not acct:
        return HTMLResponse("Account not found", status_code=404)

    balance_result = get_account_balance_rich(
        db, account_id, target_currency=acct.currency,
    )
    balance = balance_result.value
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

    # Category spending summary for this account. Exclude transfers — money
    # moved between your own accounts (the "Account Transfer" category or any
    # TRANSFER-type category, plus anything flagged is_transfer) is not
    # spending and shouldn't appear here.
    from app.models.category import CategoryType as _CatType
    cat_rows = db.execute(
        sa_select(
            Category.name,
            sa_func.count(Transaction.id).label("txn_count"),
            sa_func.sum(Transaction.amount).label("total"),
        )
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.account_id == account_id,
            Transaction.is_transfer.is_(False),
            Category.category_type != _CatType.TRANSFER,
        )
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
            Transaction.is_transfer.is_(False),
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
    since_12m = naive_utc_now() - timedelta(days=365)

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
            Transaction.is_transfer.is_(False),
            sa_func.lower(Category.name) != "account transfer",
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

    # Liability balance history (one row per statement uploaded)
    from sqlalchemy import select as _select
    from app.models.snapshots import LiabilityBalanceSnapshot
    liability_balance_history = []
    if not acct.is_asset:
        snaps = db.execute(
            _select(LiabilityBalanceSnapshot)
            .where(LiabilityBalanceSnapshot.account_id == acct.id)
            .order_by(LiabilityBalanceSnapshot.as_of_date)
        ).scalars().all()
        liability_balance_history = [
            {"date": s.as_of_date.strftime("%Y-%m-%d"), "balance": float(s.value_native)}
            for s in snaps
        ]

    # Mortgage payoff projection data
    mortgage_payoff = None
    if acct.account_type.value == "mortgage":
        current_balance = abs(float(balance)) if balance else 0.0
        # Use statement_balance if available and more accurate
        if acct.statement_balance and float(acct.statement_balance) > 0:
            current_balance = float(acct.statement_balance)
        rate = float(acct.interest_rate) if acct.interest_rate else None
        payment = float(acct.monthly_payment) if acct.monthly_payment else None
        if current_balance > 0 and rate and payment and payment > 0:
            monthly_rate = rate / 12.0
            if monthly_rate > 0 and payment > current_balance * monthly_rate:
                # Build amortization schedule (cap at 480 months = 40 years)
                schedule = []
                bal = current_balance
                total_interest = 0.0
                from datetime import date
                month_dt = date.today().replace(day=1)
                import calendar
                for _ in range(480):
                    interest = bal * monthly_rate
                    principal = payment - interest
                    if principal <= 0:
                        break
                    bal -= principal
                    total_interest += interest
                    if bal < 0:
                        bal = 0.0
                    # advance one month
                    yr, mo = month_dt.year, month_dt.month
                    if mo == 12:
                        month_dt = month_dt.replace(year=yr + 1, month=1)
                    else:
                        month_dt = month_dt.replace(month=mo + 1)
                    schedule.append({
                        "label": month_dt.strftime("%b %Y"),
                        "balance": round(bal, 2),
                        "interest": round(interest, 2),
                        "principal": round(principal, 2),
                    })
                    if bal <= 0:
                        break
                payoff_date = schedule[-1]["label"] if schedule else None
                mortgage_payoff = {
                    "current_balance": current_balance,
                    "interest_rate_pct": round(rate * 100, 4),
                    "monthly_payment": payment,
                    "payoff_date": payoff_date,
                    "total_interest_remaining": round(total_interest, 2),
                    "schedule": schedule,
                }

    # ── Payment breakdown for mortgage accounts ───────────────────────────────
    payment_breakdown: dict = {}
    interest_ytd: Decimal = Decimal("0")
    if acct.account_type.value == "mortgage":
        from sqlalchemy import select as _select2, func as _func2
        from app.models.payment_decomposition import PaymentDecomposition
        from app.models.enums import PaymentComponent

        decomp_rows = db.execute(
            _select2(
                Transaction.id.label("txn_id"),
                Transaction.date,
                Transaction.description,
                Transaction.amount,
                PaymentDecomposition.component,
                PaymentDecomposition.amount.label("comp_amount"),
            )
            .join(PaymentDecomposition, PaymentDecomposition.transaction_id == Transaction.id)
            .where(Transaction.account_id == acct.id)
            .order_by(Transaction.date.desc())
            .limit(72)  # up to 24 payments × 3 components
        ).all()

        for row in decomp_rows:
            txn_id = row.txn_id
            if txn_id not in payment_breakdown:
                payment_breakdown[txn_id] = {
                    "date": row.date.strftime("%Y-%m-%d"),
                    "description": row.description,
                    "amount": float(row.amount),
                    "principal": 0.0,
                    "interest": 0.0,
                    "escrow": 0.0,
                }
            if row.component == PaymentComponent.PRINCIPAL.value:
                payment_breakdown[txn_id]["principal"] = float(row.comp_amount)
            elif row.component == PaymentComponent.INTEREST.value:
                payment_breakdown[txn_id]["interest"] = float(row.comp_amount)
            elif row.component == PaymentComponent.ESCROW.value:
                payment_breakdown[txn_id]["escrow"] = float(row.comp_amount)

        ytd_start = datetime(naive_utc_now().year, 1, 1)
        interest_ytd = db.execute(
            _select2(_func2.sum(PaymentDecomposition.amount))
            .join(Transaction, PaymentDecomposition.transaction_id == Transaction.id)
            .where(
                Transaction.account_id == acct.id,
                PaymentDecomposition.component == PaymentComponent.INTEREST.value,
                Transaction.date >= ytd_start,
            )
        ).scalar() or Decimal("0")

    # Plan-It plans (Amex BA) — order by start date so the table reads like
    # the statement; totals are computed in the template.
    from app.models.plan_it_plan import PlanItPlan
    plan_it_plans = db.execute(
        sa_select(PlanItPlan)
        .where(PlanItPlan.account_id == account_id)
        .order_by(PlanItPlan.start_date.asc(), PlanItPlan.id.asc())
    ).scalars().all()

    # Cash-flow forecast for this account — projected balance from its
    # active scheduled payments. User-selectable horizon, default 6 months.
    from app.services.forecast_service import build_forecast
    forecast_months = max(1, min(forecast_months, 24))
    account_forecast = None
    try:
        _fc = build_forecast(db, months=forecast_months, account_ids={account_id})
        account_forecast = _fc.accounts[0] if _fc.accounts else None
    except Exception:
        account_forecast = None  # scheduled_payments table may be absent on old DBs

    # Future-dated rows now count towards the balance (a confirmed scheduled
    # payment is a real ledger entry), so say so under the headline figure
    # rather than letting the number quietly mean two different things.
    _future = db.execute(
        sa_select(
            sa_func.count(Transaction.id),
            sa_func.coalesce(sa_func.sum(Transaction.amount), 0),
        ).where(
            Transaction.account_id == account_id,
            Transaction.date > naive_utc_now(),
        )
    ).one()
    future_txn_count = _future[0] or 0
    future_txn_total = Decimal(str(_future[1] or 0))
    balance_today = balance - future_txn_total if future_txn_count else balance

    # Scheduled payments for this account, managed inline further down the
    # page. The forecast above is built from exactly these rows, so amending
    # one and watching the projection move shouldn't need a trip to /scheduled.
    from app.models.scheduled_payment import ScheduledPayment
    from app.routers.scheduled_payments import FREQUENCIES as SCHEDULED_FREQUENCIES
    try:
        account_scheduled = db.execute(
            sa_select(ScheduledPayment)
            .where(ScheduledPayment.account_id == account_id)
            .order_by(
                ScheduledPayment.active.desc(), ScheduledPayment.next_due_date,
            )
        ).scalars().all()
    except Exception:
        account_scheduled = []

    scheduled_categories = db.execute(
        sa_select(Category).order_by(Category.name)
    ).scalars().all()

    # Earliest projected date per payment. Confirming any later occurrence
    # silently skips the ones before it, so the template warns on those.
    forecast_first_occurrence: dict[int, object] = {}
    if account_forecast:
        for _evt in account_forecast.events:      # already sorted by date
            forecast_first_occurrence.setdefault(_evt.scheduled_payment_id, _evt.date)

    # RSU grants + vesting (for RSU accounts) and pension fund holdings.
    rsu_grants = None
    rsu_summary = None
    pension_holdings = None
    if acct.account_type == AccountType.RSU:
        from app.models.rsu import RSUGrant
        from app.services.rsu_service import value_rsu_account
        rsu_grants = db.execute(
            sa_select(RSUGrant)
            .where(RSUGrant.account_id == account_id)
            .order_by(RSUGrant.award_date.desc())
        ).scalars().all()
        if rsu_grants:
            try:
                rsu_summary = value_rsu_account(
                    db, acct, refresh_price=False, persist=False,
                )
            except Exception:
                rsu_summary = None
    elif acct.account_type == AccountType.PENSION:
        from app.services.epa_pension_import import value_pension_account
        try:
            _pv = value_pension_account(db, acct, persist=False)
            pension_holdings = _pv.get("holdings") or None
        except Exception:
            pension_holdings = None

    # Brokerage activity lives in its own tables (the IBKR importer writes
    # trades / dividends / lots, not generic Transaction rows), so surface it
    # here — otherwise these accounts look empty on their own page.
    broker_positions = broker_trades = broker_dividends = None
    value_history = None
    if acct.account_type in _MARKET_ACCOUNT_TYPES:
        from app.models.instrument import Instrument, PositionLot
        from app.models.stock_dividend import StockDividend
        from app.models.stock_trade import StockTrade
        try:
            broker_positions = db.execute(
                sa_select(PositionLot, Instrument)
                .join(Instrument, PositionLot.instrument_id == Instrument.id)
                .where(
                    PositionLot.account_id == account_id,
                    PositionLot.source != "epa_pension",
                )
                .order_by(Instrument.symbol)
            ).all() or None
            broker_trades = db.execute(
                sa_select(StockTrade, Instrument)
                .join(Instrument, StockTrade.instrument_id == Instrument.id)
                .where(StockTrade.account_id == account_id)
                .order_by(StockTrade.trade_date.desc())
                .limit(50)
            ).all() or None
            broker_dividends = db.execute(
                sa_select(StockDividend, Instrument)
                .join(Instrument, StockDividend.instrument_id == Instrument.id)
                .where(StockDividend.account_id == account_id)
                .order_by(StockDividend.pay_date.desc())
                .limit(50)
            ).all() or None
        except Exception:
            pass

        # Value over time, split per holding, with cumulative dividends.
        try:
            from app.services.price_service import compute_account_value_by_symbol
            _vh = compute_account_value_by_symbol(db, account_id)
            if _vh.get("dates"):
                value_history = _vh
        except Exception:
            log.warning("account_detail: value history failed for %s", account_id)

    return templates.TemplateResponse(request, "accounts/detail.html", {
        "account": acct,
        "balance": balance,
        "account_forecast": account_forecast,
        "forecast_months": forecast_months,
        "future_txn_count": future_txn_count,
        "future_txn_total": future_txn_total,
        "balance_today": balance_today,
        "account_scheduled": account_scheduled,
        "scheduled_categories": scheduled_categories,
        "scheduled_frequencies": SCHEDULED_FREQUENCIES,
        "forecast_first_occurrence": forecast_first_occurrence,
        "rsu_grants": rsu_grants,
        "rsu_summary": rsu_summary,
        "pension_holdings": pension_holdings,
        "broker_positions": broker_positions,
        "broker_trades": broker_trades,
        "broker_dividends": broker_dividends,
        "value_history": value_history,
        "balance_source": balance_result.balance_source_used,
        "balance_stale": balance_result.balance_stale,
        "transactions": recent_txns,
        "split_categories": split_categories,
        "total_transactions": total_txn_count,
        "category_summary": category_summary,
        "monthly_spend_labels": monthly_spend_labels,
        "monthly_spend_datasets": monthly_spend_datasets,
        "prop_status": prop_status,
        "mortgage_account": mortgage_account,
        "mortgage_balance": mortgage_balance,
        "mortgage_payoff": mortgage_payoff,
        "liability_balance_history": liability_balance_history,
        "payment_breakdown": payment_breakdown,
        "interest_ytd": interest_ytd,
        "plan_it_plans": plan_it_plans,
        "payment_due": next_payment_due(db, acct),
        "now": naive_utc_now(),
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
    interest_rate: str = Form(""),
    monthly_payment: str = Form(""),
    statement_balance: str = Form(""),
    statement_balance_as_of: str = Form(""),
    overdraft_limit: str = Form(""),
    overdraft_as_of: str = Form(""),
    payment_due_date: str = Form(""),
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
        value_as_of_date=naive_utc_now() if val is not None else None,
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
            acct.value_as_of_date = naive_utc_now()
        elif acct.purchase_price and not acct.current_value:
            acct.current_value = acct.purchase_price
            acct.value_as_of_date = naive_utc_now()
        db.commit()

        # Re-fetch estimated value when address changes and no manual value set
        if acct.property_address and not val:
            _try_fetch_property_value(db, acct)
    elif acct_type in _PHYSICAL_ASSET_TYPES:
        acct.balance_truth_source = "manual_mark"
        if val:
            acct.current_value = val
            acct.value_as_of_date = naive_utc_now()
        elif acct.purchase_price and not acct.current_value:
            acct.current_value = acct.purchase_price
            acct.value_as_of_date = naive_utc_now()
        db.commit()

    if acct_type == AccountType.MORTGAGE:
        if interest_rate.strip():
            try:
                acct.interest_rate = float(interest_rate) / 100.0
            except ValueError:
                pass
        elif not interest_rate.strip():
            acct.interest_rate = None
        if monthly_payment.strip():
            try:
                acct.monthly_payment = Decimal(monthly_payment)
            except (ValueError, InvalidOperation) as exc:
                log.warning(
                    "account_form: monthly_payment parse failed (%r): %s",
                    monthly_payment, exc,
                )
        elif not monthly_payment.strip():
            acct.monthly_payment = None
        db.commit()

    _INTEREST_TYPES = {AccountType.CREDIT_CARD, AccountType.LOAN, AccountType.OTHER}
    if acct_type in _INTEREST_TYPES:
        if interest_rate.strip():
            try:
                acct.interest_rate = float(interest_rate) / 100.0
            except ValueError:
                pass
        else:
            acct.interest_rate = None
        db.commit()

    if statement_balance.strip():
        try:
            acct.statement_balance = Decimal(statement_balance)
            acct.statement_balance_as_of = (
                datetime.strptime(statement_balance_as_of.strip(), "%Y-%m-%d")
                if statement_balance_as_of.strip()
                else naive_utc_now()
            )
            if acct.balance_truth_source in (None, "transaction_sum"):
                acct.balance_truth_source = "hybrid"
            db.commit()
        except (ValueError, Exception):
            pass
    elif acct.statement_balance is not None and not statement_balance.strip():
        # User cleared the field — remove it
        acct.statement_balance = None
        acct.statement_balance_as_of = None
        if acct.balance_truth_source == "hybrid":
            acct.balance_truth_source = "transaction_sum"
        db.commit()

    if overdraft_limit.strip():
        try:
            acct.overdraft_limit = Decimal(overdraft_limit)
            acct.overdraft_as_of = (
                datetime.strptime(overdraft_as_of.strip(), "%Y-%m-%d")
                if overdraft_as_of.strip() else naive_utc_now()
            )
            db.commit()
        except (ValueError, InvalidOperation):
            db.rollback()
    elif acct.overdraft_limit is not None and not overdraft_limit.strip():
        acct.overdraft_limit = None
        acct.overdraft_as_of = None
        db.commit()

    if acct_type in LIABILITY_TYPES and payment_due_date.strip():
        try:
            acct.payment_due_date = datetime.strptime(
                payment_due_date.strip(), "%Y-%m-%d"
            ).date()
            db.commit()
        except ValueError:
            db.rollback()
    elif acct.payment_due_date is not None and not payment_due_date.strip():
        acct.payment_due_date = None
        db.commit()

    return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)


@router.post("/{account_id}/accrue-interest")
def account_accrue_interest(
    account_id: int,
    start_date: str = Form(""),
    through_date: str = Form(""),
    db: Session = Depends(get_db),
):
    """Backfill monthly interest accruals on an interest-bearing account."""
    from datetime import date as _date
    from app.services.interest_accrual import accrue_interest

    acct = get_account(db, account_id)
    if not acct:
        return HTMLResponse("Account not found", status_code=404)

    start = None
    through = None
    try:
        if start_date.strip():
            start = datetime.strptime(start_date.strip(), "%Y-%m-%d").date()
        if through_date.strip():
            through = datetime.strptime(through_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        pass

    created = accrue_interest(db, acct, start=start, through=through or _date.today())
    db.commit()
    return RedirectResponse(
        url=f"/accounts/{account_id}?accrued={len(created)}", status_code=303,
    )


@router.post("/{account_id}/close")
def account_close(
    account_id: int,
    closed_at: str = Form(""),
    reason: str = Form(""),
    zero_balance: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Close an account: keeps every transaction, hides it from the active
    list, and stops its scheduled payments."""
    acct = get_account(db, account_id)
    if not acct:
        return HTMLResponse("Account not found", status_code=404)

    when = None
    if closed_at.strip():
        try:
            when = datetime.strptime(closed_at.strip(), "%Y-%m-%d").date()
        except ValueError:
            return RedirectResponse(
                url=f"/accounts/{account_id}?close_err=date", status_code=303,
            )

    stats = close_account(
        db, acct, closed_at=when, reason=reason, zero_balance=zero_balance,
    )
    return RedirectResponse(
        url=f"/accounts/{account_id}?closed=1&sched={stats['scheduled_deactivated']}",
        status_code=303,
    )


@router.post("/{account_id}/reopen")
def account_reopen(account_id: int, db: Session = Depends(get_db)):
    acct = get_account(db, account_id)
    if not acct:
        return HTMLResponse("Account not found", status_code=404)
    reopen_account(db, acct)
    return RedirectResponse(url=f"/accounts/{account_id}?reopened=1", status_code=303)


@router.post("/{account_id}/refresh-prices")
def account_refresh_prices(account_id: int, db: Session = Depends(get_db)):
    """Fetch live market prices for the holdings in a brokerage account."""
    acct = get_account(db, account_id)
    if not acct or acct.account_type not in _MARKET_ACCOUNT_TYPES:
        return HTMLResponse("Not a market account", status_code=404)

    from sqlalchemy import select as sa_select
    from app.models.instrument import Instrument, PositionLot
    from app.services.price_service import get_current_prices

    symbols = [
        s for (s,) in db.execute(
            sa_select(Instrument.symbol)
            .join(PositionLot, PositionLot.instrument_id == Instrument.id)
            .where(
                PositionLot.account_id == account_id,
                PositionLot.source != "epa_pension",
            )
            .distinct()
        ).all()
    ]
    if not symbols:
        return RedirectResponse(
            url=f"/accounts/{account_id}?priced=0", status_code=303,
        )
    try:
        prices, _as_of, live = get_current_prices(symbols, db=db)
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(
            url=f"/accounts/{account_id}?price_err=1", status_code=303,
        )
    return RedirectResponse(
        url=f"/accounts/{account_id}?priced={len(prices)}&live={1 if live else 0}",
        status_code=303,
    )


@router.post("/{account_id}/refresh-rsu-price")
def account_refresh_rsu_price(account_id: int, db: Session = Depends(get_db)):
    """Fetch the live market price and re-value an RSU account."""
    acct = get_account(db, account_id)
    if not acct or acct.account_type != AccountType.RSU:
        return HTMLResponse("Not an RSU account", status_code=404)
    from app.services.rsu_service import value_rsu_account
    try:
        summary = value_rsu_account(db, acct, refresh_price=True, persist=True)
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(url=f"/accounts/{account_id}?rsu_price_err=1", status_code=303)
    px = summary.get("price")
    return RedirectResponse(
        url=f"/accounts/{account_id}?rsu_priced={px if px is not None else ''}",
        status_code=303,
    )


@router.post("/{account_id}/delete")
def account_remove(account_id: int, db: Session = Depends(get_db)):
    delete_account(db, account_id)
    return RedirectResponse(url="/accounts", status_code=303)


@router.get("/{account_id}/valuation-picker", response_class=HTMLResponse)
def valuation_picker(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
):
    """Fetch all available estimates and let the user choose one."""
    from app.services.property_valuation import estimate_all_providers

    acct = get_account(db, account_id)
    if not acct or not acct.property_address:
        return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)

    profile = get_profile(db)
    estimates = estimate_all_providers(
        address=acct.property_address,
        currency=acct.currency,
        country_of_residence=profile.country_of_residence,
        rentcast_api_key=profile.rentcast_api_key,
        property_data_api_key=profile.property_data_api_key,
        domain_api_key=profile.domain_api_key,
    )

    addr_enc = urllib.parse.quote(acct.property_address, safe="")
    external_links = [
        ("Zoopla sold prices", f"https://www.zoopla.co.uk/house-prices/{addr_enc}/"),
        ("Rightmove sold prices", f"https://www.rightmove.co.uk/house-prices/{addr_enc}.html"),
        ("Zoopla free valuation", f"https://www.zoopla.co.uk/valuation/{addr_enc}/"),
    ]

    return templates.TemplateResponse(request, "accounts/valuation_picker.html", {
        "account": acct,
        "estimates": estimates,
        "external_links": external_links,
    })


@router.post("/{account_id}/apply-valuation")
def apply_valuation(
    account_id: int,
    value: Decimal = Form(...),
    source: str = Form("manual"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save a chosen valuation (from picker or manual entry)."""
    from app.models.asset_valuation import AssetValuation

    acct = get_account(db, account_id)
    if not acct:
        return RedirectResponse(url="/accounts", status_code=303)

    val = AssetValuation(
        account_id=acct.id,
        date=naive_utc_now(),
        value=value,
        currency=acct.currency,
        source=source,
        notes=notes,
    )
    db.add(val)
    acct.current_value = value
    acct.value_as_of_date = naive_utc_now()
    acct.balance_truth_source = "manual_mark"
    db.commit()

    return RedirectResponse(url=f"/accounts/{account_id}?value_refreshed=1", status_code=303)


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
        date=naive_utc_now(),
        value=result.value,
        currency=acct.currency,
        source=result.source,
        notes=result.notes or result.source_label,
    )
    db.add(val)
    acct.current_value = result.value
    acct.value_as_of_date = naive_utc_now()
    acct.balance_truth_source = "manual_mark"
    db.commit()
