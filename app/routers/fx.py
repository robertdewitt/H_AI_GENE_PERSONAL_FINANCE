from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates
from app.models.currency_rate import CurrencyRate
from app.services.fx_service import (
    COMMON_CURRENCIES,
    convert_amount,
    list_available_pairs,
    upsert_rate,
)
from app.services.fx_rate_fetcher import (
    sync_current_rates,
    sync_historical_rates,
)

router = APIRouter(prefix="/fx", tags=["fx"])


@router.get("", response_class=HTMLResponse)
def fx_page(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import func as sa_func

    pairs = list_available_pairs(db)
    recent = db.execute(
        select(CurrencyRate)
        .order_by(CurrencyRate.date.desc())
        .limit(50)
    ).scalars().all()

    base = "USD"
    key_quotes = ["GBP", "EUR", "JPY"]
    bootstrap_status = {}
    for q in key_quotes:
        count = db.execute(
            select(sa_func.count(CurrencyRate.id)).where(
                CurrencyRate.base_currency == base,
                CurrencyRate.quote_currency == q,
            )
        ).scalar() or 0
        bootstrap_status[f"{base}/{q}"] = {"count": count}

    return templates.TemplateResponse(request, "fx/dashboard.html", {
        "pairs": pairs,
        "recent_rates": recent,
        "currencies": COMMON_CURRENCIES,
        "fetch_result": None,
        "bootstrap_status": bootstrap_status,
    })


@router.post("/add")
def fx_add_rate(
    base_currency: str = Form(...),
    quote_currency: str = Form(...),
    date: str = Form(...),
    rate: float = Form(...),
    source: str = Form("manual"),
    db: Session = Depends(get_db),
):
    upsert_rate(
        db,
        base_currency=base_currency.upper(),
        quote_currency=quote_currency.upper(),
        date=datetime.strptime(date, "%Y-%m-%d"),
        rate=rate,
        source=source,
    )
    return RedirectResponse(url="/fx", status_code=303)


@router.post("/fetch-current")
def fx_fetch_current(
    request: Request,
    base_currency: str = Form("USD"),
    db: Session = Depends(get_db),
):
    """Pull today's rates from Yahoo Finance / ECB for all common currencies."""
    statuses = sync_current_rates(db, base=base_currency.upper())

    pairs = list_available_pairs(db)
    recent = db.execute(
        select(CurrencyRate)
        .order_by(CurrencyRate.date.desc())
        .limit(50)
    ).scalars().all()

    succeeded = sum(1 for v in statuses.values() if v != "failed")
    failed = sum(1 for v in statuses.values() if v == "failed")

    return templates.TemplateResponse(request, "fx/dashboard.html", {
        "pairs": pairs,
        "recent_rates": recent,
        "currencies": COMMON_CURRENCIES,
        "fetch_result": {
            "type": "current",
            "base": base_currency.upper(),
            "succeeded": succeeded,
            "failed": failed,
            "details": statuses,
        },
    })


@router.post("/fetch-historical")
def fx_fetch_historical(
    request: Request,
    base_currency: str = Form(...),
    quote_currency: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(""),
    db: Session = Depends(get_db),
):
    """Pull historical daily rates for a specific pair."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date.strip() else None

    count = sync_historical_rates(
        db,
        base=base_currency.upper(),
        quote=quote_currency.upper(),
        start_date=start,
        end_date=end,
    )

    pairs = list_available_pairs(db)
    recent = db.execute(
        select(CurrencyRate)
        .order_by(CurrencyRate.date.desc())
        .limit(50)
    ).scalars().all()

    return templates.TemplateResponse(request, "fx/dashboard.html", {
        "pairs": pairs,
        "recent_rates": recent,
        "currencies": COMMON_CURRENCIES,
        "fetch_result": {
            "type": "historical",
            "base": base_currency.upper(),
            "quote": quote_currency.upper(),
            "count": count,
        },
    })


@router.get("/convert", response_class=HTMLResponse)
def fx_convert_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "fx/convert.html", {
        "currencies": COMMON_CURRENCIES,
        "result": None,
    })


@router.post("/convert")
def fx_convert(
    request: Request,
    amount: float = Form(...),
    from_currency: str = Form(...),
    to_currency: str = Form(...),
    date: str = Form(...),
    db: Session = Depends(get_db),
):
    dt = datetime.strptime(date, "%Y-%m-%d")
    converted, rate = convert_amount(
        db, amount, from_currency.upper(), to_currency.upper(), dt
    )
    return templates.TemplateResponse(request, "fx/convert.html", {
        "currencies": COMMON_CURRENCIES,
        "result": {
            "amount": amount,
            "from": from_currency.upper(),
            "to": to_currency.upper(),
            "date": date,
            "converted": converted,
            "rate": rate,
        },
    })
