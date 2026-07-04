"""Scheduled / recurring payments — CRUD, pattern detection, and forecast."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.account import Account
from app.models.category import Category
from app.models.scheduled_payment import ScheduledPayment
from app.templating import templates

router = APIRouter(prefix="/scheduled", tags=["scheduled"])

FREQUENCIES = ["weekly", "biweekly", "monthly", "quarterly", "annually", "once"]
AMOUNT_TYPES = ["fixed", "estimated", "variable"]


def _all_accounts(db: Session) -> list[Account]:
    return db.execute(select(Account).order_by(Account.name)).scalars().all()


def _all_categories(db: Session) -> list[Category]:
    return db.execute(select(Category).order_by(Category.name)).scalars().all()


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def scheduled_list(request: Request, db: Session = Depends(get_db)):
    payments = db.execute(
        select(ScheduledPayment)
        .order_by(ScheduledPayment.active.desc(), ScheduledPayment.next_due_date)
    ).scalars().all()

    # Effective flag level (auto vs reminder) per payment, and which payments
    # are suppressed as the source side of an inter-account transfer.
    from app.services.payment_classifier import (
        effective_flag_level, find_suppressed_transfer_ids,
    )
    accounts_map = {p.account_id: p.account for p in payments if p.account}
    levels = {p.id: effective_flag_level(p, accounts_map.get(p.account_id)) for p in payments}
    suppressed = find_suppressed_transfer_ids(
        [p for p in payments if p.active], accounts_map,
    )
    return templates.TemplateResponse(request, "scheduled/list.html", {
        "payments": payments,
        "today": date.today(),
        "levels": levels,
        "suppressed": suppressed,
    })


# ── New / Create ──────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
def scheduled_new(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "scheduled/form.html", {
        "payment": None,
        "accounts": _all_accounts(db),
        "categories": _all_categories(db),
        "frequencies": FREQUENCIES,
        "amount_types": AMOUNT_TYPES,
        "today": date.today().isoformat(),
    })


@router.post("/new")
def scheduled_create(
    request: Request,
    description: str = Form(...),
    amount: str = Form(...),
    amount_type: str = Form("fixed"),
    currency: str = Form("USD"),
    account_id: int = Form(...),
    category_id: str = Form(""),
    frequency: str = Form("monthly"),
    next_due_date: str = Form(...),
    end_date: str = Form(""),
    day_of_month: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    payment = ScheduledPayment(
        description=description.strip(),
        amount=Decimal(amount),
        amount_type=amount_type,
        currency=currency,
        account_id=account_id,
        category_id=int(category_id) if category_id.strip() else None,
        frequency=frequency,
        next_due_date=date.fromisoformat(next_due_date),
        end_date=date.fromisoformat(end_date) if end_date.strip() else None,
        day_of_month=int(day_of_month) if day_of_month.strip() else None,
        notes=notes.strip() or None,
        source="manual",
        active=True,
    )
    db.add(payment)
    db.commit()
    return RedirectResponse(url="/scheduled", status_code=303)


# ── Edit / Update ─────────────────────────────────────────────────────────────

@router.get("/{payment_id}/edit", response_class=HTMLResponse)
def scheduled_edit(payment_id: int, request: Request, db: Session = Depends(get_db)):
    payment = db.get(ScheduledPayment, payment_id)
    if not payment:
        return RedirectResponse(url="/scheduled", status_code=303)
    return templates.TemplateResponse(request, "scheduled/form.html", {
        "payment": payment,
        "accounts": _all_accounts(db),
        "categories": _all_categories(db),
        "frequencies": FREQUENCIES,
        "amount_types": AMOUNT_TYPES,
        "today": date.today().isoformat(),
    })


@router.post("/{payment_id}/edit")
def scheduled_update(
    payment_id: int,
    request: Request,
    description: str = Form(...),
    amount: str = Form(...),
    amount_type: str = Form("fixed"),
    currency: str = Form("USD"),
    account_id: int = Form(...),
    category_id: str = Form(""),
    frequency: str = Form("monthly"),
    next_due_date: str = Form(...),
    end_date: str = Form(""),
    day_of_month: str = Form(""),
    notes: str = Form(""),
    active: str = Form("on"),
    db: Session = Depends(get_db),
):
    payment = db.get(ScheduledPayment, payment_id)
    if not payment:
        return RedirectResponse(url="/scheduled", status_code=303)

    payment.description = description.strip()
    payment.amount = Decimal(amount)
    payment.amount_type = amount_type
    payment.currency = currency
    payment.account_id = account_id
    payment.category_id = int(category_id) if category_id.strip() else None
    payment.frequency = frequency
    payment.next_due_date = date.fromisoformat(next_due_date)
    payment.end_date = date.fromisoformat(end_date) if end_date.strip() else None
    payment.day_of_month = int(day_of_month) if day_of_month.strip() else None
    payment.notes = notes.strip() or None
    payment.active = (active == "on")
    db.commit()
    return RedirectResponse(url="/scheduled", status_code=303)


# ── Set flag level (auto vs reminder) ──────────────────────────────────────────

@router.post("/{payment_id}/flag-level")
def scheduled_set_flag_level(
    payment_id: int,
    flag_level: str = Form(...),  # "auto" | "reminder" | "default"
    db: Session = Depends(get_db),
):
    payment = db.get(ScheduledPayment, payment_id)
    if payment:
        payment.flag_level = flag_level if flag_level in ("auto", "reminder") else None
        db.commit()
    return RedirectResponse(url="/scheduled", status_code=303)


# ── Toggle active ─────────────────────────────────────────────────────────────

@router.post("/{payment_id}/toggle")
def scheduled_toggle(payment_id: int, db: Session = Depends(get_db)):
    payment = db.get(ScheduledPayment, payment_id)
    if payment:
        payment.active = not payment.active
        db.commit()
    return RedirectResponse(url="/scheduled", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{payment_id}/delete")
def scheduled_delete(payment_id: int, db: Session = Depends(get_db)):
    payment = db.get(ScheduledPayment, payment_id)
    if payment:
        db.delete(payment)
        db.commit()
    return RedirectResponse(url="/scheduled", status_code=303)


# ── Detect recurring from history ─────────────────────────────────────────────

@router.get("/detect", response_class=HTMLResponse)
def detect_page(request: Request, db: Session = Depends(get_db)):
    from app.services.recurring_detector import detect_recurring_payments
    suggestions = detect_recurring_payments(db)
    # Filter out ones already in scheduled_payments (by description + account)
    existing = db.execute(select(ScheduledPayment)).scalars().all()
    existing_keys = {
        (p.account_id, p.description.lower().strip()) for p in existing
    }
    suggestions = [
        s for s in suggestions
        if (s["account_id"], s["description"].lower().strip()) not in existing_keys
    ]
    accounts = {a.id: a for a in _all_accounts(db)}
    categories = _all_categories(db)
    return templates.TemplateResponse(request, "scheduled/detect.html", {
        "suggestions": suggestions,
        "accounts": accounts,
        "categories": categories,
        "frequencies": FREQUENCIES,
    })


@router.post("/detect/confirm")
def detect_confirm(
    request: Request,
    db: Session = Depends(get_db),
    descriptions: list[str] = Form(default=[]),
    amounts: list[str] = Form(default=[]),
    amount_types: list[str] = Form(default=[]),
    currencies: list[str] = Form(default=[]),
    account_ids: list[str] = Form(default=[]),
    category_ids: list[str] = Form(default=[]),
    frequencies: list[str] = Form(default=[]),
    next_due_dates: list[str] = Form(default=[]),
):
    # Pad amount_types so older clients (no hidden field) still work.
    if len(amount_types) < len(descriptions):
        amount_types = list(amount_types) + ["fixed"] * (
            len(descriptions) - len(amount_types)
        )
    added = 0
    for desc, amt, at, cur, acct, cat, freq, ndd in zip(
        descriptions, amounts, amount_types, currencies, account_ids,
        category_ids, frequencies, next_due_dates,
    ):
        if not desc.strip():
            continue
        db.add(ScheduledPayment(
            description=desc.strip(),
            amount=Decimal(amt),
            amount_type=at.strip() or "fixed",
            currency=cur,
            account_id=int(acct),
            category_id=int(cat) if cat.strip() else None,
            frequency=freq,
            next_due_date=date.fromisoformat(ndd),
            source="auto_detected",
            confidence=0.85,
            active=True,
        ))
        added += 1
    db.commit()
    return RedirectResponse(url=f"/scheduled?detected={added}", status_code=303)


# ── Forecast ──────────────────────────────────────────────────────────────────

@router.get("/forecast", response_class=HTMLResponse)
def forecast(
    request: Request,
    months: int = 3,
    db: Session = Depends(get_db),
):
    from app.services.forecast_service import build_forecast
    months = max(1, min(months, 12))
    forecast_data = build_forecast(db, months=months)
    return templates.TemplateResponse(request, "scheduled/forecast.html", {
        "forecast": forecast_data,
        "months": months,
        "today": date.today(),
    })
