"""Portfolio dashboard — positions, trades, dividends, and performance chart."""
from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.account import Account, AccountType
from app.models.instrument import Instrument, PositionLot
from app.models.stock_dividend import StockDividend
from app.models.stock_trade import StockTrade
from app.services.price_service import compute_portfolio_history, get_current_prices
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

_INVESTMENT_TYPES = {
    AccountType.BROKERAGE,
    AccountType.IRA,
    AccountType.ROTH_IRA,
    AccountType.PENSION,
    AccountType.FOUR_OH_ONE_K,
}

_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
    "CAD": "C$", "AUD": "A$", "CHF": "Fr", "CNY": "¥",
    "INR": "₹", "BRL": "R$",
}


@router.get("", response_class=HTMLResponse)
def portfolio_dashboard(request: Request, db: Session = Depends(get_db)):
    # ── Fetch all investment accounts ─────────────────────────────────────
    investment_accounts = db.execute(
        select(Account).where(Account.account_type.in_(_INVESTMENT_TYPES))
        .order_by(Account.name)
    ).scalars().all()

    # ── Build positions list ──────────────────────────────────────────────
    position_rows = db.execute(
        select(PositionLot, Instrument)
        .join(Instrument, PositionLot.instrument_id == Instrument.id)
        .where(
            PositionLot.account_id.in_([a.id for a in investment_accounts])
        )
        .order_by(Instrument.symbol)
    ).all()

    # Aggregate by symbol (multiple lots / accounts → single display row)
    agg: dict[str, dict] = {}
    for lot, inst in position_rows:
        sym = inst.symbol
        if sym not in agg:
            agg[sym] = {
                "symbol": sym,
                "name": inst.name or sym,
                "qty": 0.0,
                "cost_basis_total": Decimal("0.00"),
                "currency": inst.currency or "USD",
            }
        agg[sym]["qty"] += float(lot.quantity)
        cb = lot.cost_basis_total or Decimal("0.00")
        agg[sym]["cost_basis_total"] += cb

    symbols_held = list(agg.keys())

    # ── Fetch current prices (with DB fallback + caching) ────────────────
    if symbols_held:
        current_prices, prices_as_of, prices_live = get_current_prices(symbols_held, db=db)
    else:
        current_prices, prices_as_of, prices_live = {}, {}, False

    # Oldest timestamp among returned prices = "data as of" time shown to user
    prices_timestamp = min(prices_as_of.values()) if prices_as_of else None

    # ── Build enriched position dicts ────────────────────────────────────
    positions = []
    total_current_value = Decimal("0.00")
    total_cost_basis = Decimal("0.00")
    total_unrealized_pnl = Decimal("0.00")

    for sym, data in agg.items():
        qty = data["qty"]
        cb = data["cost_basis_total"]
        current_price = current_prices.get(sym, 0.0)
        current_value = Decimal(str(round(qty * current_price, 2)))
        unrealized_pnl = current_value - cb
        cost_price_avg = (cb / Decimal(str(qty))) if qty != 0 else Decimal("0.00")
        unrealized_pnl_pct = (
            float(unrealized_pnl / cb * 100) if cb != Decimal("0.00") else 0.0
        )

        positions.append({
            "symbol": sym,
            "name": data["name"],
            "qty": qty,
            "cost_basis_total": cb,
            "cost_price_avg": cost_price_avg,
            "current_price": current_price,
            "current_value": current_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        })

        total_current_value += current_value
        total_cost_basis += cb
        total_unrealized_pnl += unrealized_pnl

    positions.sort(key=lambda p: float(p["current_value"]), reverse=True)

    # ── Realized P&L ─────────────────────────────────────────────────────
    realized_row = db.execute(
        select(func.sum(StockTrade.realized_pnl))
    ).scalar()
    total_realized_pnl = Decimal(str(realized_row)) if realized_row is not None else Decimal("0.00")

    # ── Dividends ─────────────────────────────────────────────────────────
    div_total_row = db.execute(
        select(func.sum(StockDividend.amount_native))
    ).scalar()
    total_dividends = Decimal(str(div_total_row)) if div_total_row is not None else Decimal("0.00")

    # ── Trade history (last 100) ──────────────────────────────────────────
    trade_rows_raw = db.execute(
        select(StockTrade, Instrument.symbol)
        .join(Instrument, StockTrade.instrument_id == Instrument.id)
        .order_by(StockTrade.trade_date.desc())
        .limit(100)
    ).all()

    trades = [
        {
            "date": t.trade_date,
            "symbol": symbol,
            "quantity": float(t.quantity),
            "price": t.price,
            "proceeds": t.proceeds,
            "realized_pnl": t.realized_pnl,
            "currency": t.currency,
        }
        for t, symbol in trade_rows_raw
    ]

    # ── Dividend history ───────────────────────────────────────────────────
    div_rows_raw = db.execute(
        select(StockDividend, Instrument.symbol)
        .join(Instrument, StockDividend.instrument_id == Instrument.id)
        .order_by(StockDividend.pay_date.desc())
    ).all()

    dividends = [
        {
            "date": d.pay_date,
            "symbol": symbol,
            "description": d.description,
            "amount": d.amount_native,
            "currency": d.currency,
        }
        for d, symbol in div_rows_raw
    ]

    # ── Portfolio value history ───────────────────────────────────────────
    portfolio_history: list[dict] = []
    accounts_with_positions = [
        a for a in investment_accounts
        if any(lot.account_id == a.id for lot, _ in position_rows)
    ]
    if accounts_with_positions and (position_rows or trade_rows_raw):
        first_account = accounts_with_positions[0]
        try:
            portfolio_history = compute_portfolio_history(db, first_account.id)
        except Exception as exc:
            log.warning("portfolio: compute_portfolio_history failed: %s", exc)

    # ── Currency symbol from first account or default ─────────────────────
    if investment_accounts:
        base_ccy = investment_accounts[0].currency or "USD"
    else:
        base_ccy = "USD"
    currency_symbol = _CURRENCY_SYMBOLS.get(base_ccy, base_ccy)

    return templates.TemplateResponse(request, "portfolio/dashboard.html", {
        "positions": positions,
        "total_current_value": total_current_value,
        "total_cost_basis": total_cost_basis,
        "total_unrealized_pnl": total_unrealized_pnl,
        "total_realized_pnl": total_realized_pnl,
        "total_dividends": total_dividends,
        "trades": trades,
        "dividends": dividends,
        "portfolio_history": portfolio_history,
        "accounts_with_positions": accounts_with_positions,
        "currency_symbol": currency_symbol,
        "prices_live": prices_live,
        "prices_timestamp": prices_timestamp,
    })


@router.get("/refresh-prices", response_class=RedirectResponse)
def refresh_prices():
    """Redirect back to portfolio — the page reload refetches live prices."""
    return RedirectResponse(url="/portfolio", status_code=303)
