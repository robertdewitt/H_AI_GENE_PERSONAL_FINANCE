"""Price service — fetch current prices and reconstruct portfolio history.

Uses yfinance for live prices and historical data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def get_current_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch the latest price for each symbol using yfinance fast_info.

    Returns a dict of {symbol: price}. Symbols that fail are omitted.
    """
    result: dict[str, float] = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            price = ticker.fast_info.last_price
            if price is not None and price > 0:
                result[symbol] = float(price)
            else:
                log.warning("price_service: no price for %s (got %r)", symbol, price)
        except Exception as exc:
            log.warning("price_service: failed to fetch price for %s: %s", symbol, exc)
    return result


def compute_portfolio_history(db: Session, account_id: int) -> list[dict]:
    """Reconstruct daily portfolio value for an account.

    Returns a list of {"date": "YYYY-MM-DD", "value": float} sorted by date.
    """
    from app.models.instrument import Instrument, PositionLot
    from app.models.stock_trade import StockTrade

    # ── Load all trades for this account ─────────────────────────────────
    trade_rows = db.execute(
        select(StockTrade, Instrument.symbol)
        .join(Instrument, StockTrade.instrument_id == Instrument.id)
        .where(StockTrade.account_id == account_id)
        .order_by(StockTrade.trade_date)
    ).all()

    # ── Load current positions ────────────────────────────────────────────
    position_rows = db.execute(
        select(PositionLot, Instrument.symbol)
        .join(Instrument, PositionLot.instrument_id == Instrument.id)
        .where(PositionLot.account_id == account_id, PositionLot.source == "ibkr")
    ).all()

    if not trade_rows and not position_rows:
        return []

    # Current quantities from PositionLot
    current_qty: dict[str, float] = {}
    for lot, symbol in position_rows:
        current_qty[symbol] = current_qty.get(symbol, 0.0) + float(lot.quantity)

    # All symbols we care about
    all_symbols = set(current_qty.keys()) | {sym for _, sym in trade_rows}

    # ── Determine date range ──────────────────────────────────────────────
    if trade_rows:
        start_date = min(t.trade_date for t, _ in trade_rows)
    else:
        start_date = datetime.now() - timedelta(days=365)

    # Give a one-day buffer so yfinance captures the first day
    start_date_dl = start_date - timedelta(days=5)

    if not all_symbols:
        return []

    # ── Download historical close prices ─────────────────────────────────
    symbols_list = sorted(all_symbols)
    try:
        raw = yf.download(
            symbols_list,
            start=start_date_dl.strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=False,
        )
    except Exception as exc:
        log.warning("price_service: yfinance download failed: %s", exc)
        return []

    if raw is None or raw.empty:
        return []

    # Extract "Close" prices. yfinance returns a MultiIndex DataFrame for
    # multiple symbols (columns like ("Close", "AAPL")) and a flat DataFrame
    # for a single symbol (column "Close").
    try:
        if hasattr(raw.columns, "levels"):
            # MultiIndex — ("Close", symbol) style
            close_df = raw["Close"]
        else:
            # Single symbol — flat columns
            close_df = raw[["Close"]]
            close_df.columns = symbols_list  # rename "Close" → symbol

        # Forward-fill prices so weekends/holidays carry last known price
        close_df = close_df.ffill()
    except Exception as exc:
        log.warning("price_service: price DataFrame processing failed: %s", exc)
        return []

    # ── Reconstruct starting positions ───────────────────────────────────
    # starting_qty[symbol] = current_qty - sum(all trades for symbol)
    trade_sum: dict[str, float] = {}
    for trade, symbol in trade_rows:
        trade_sum[symbol] = trade_sum.get(symbol, 0.0) + float(trade.quantity)

    starting_qty: dict[str, float] = {}
    for symbol in all_symbols:
        starting_qty[symbol] = current_qty.get(symbol, 0.0) - trade_sum.get(symbol, 0.0)

    # Build a dict of trade_date → list of (symbol, qty_delta)
    trades_by_date: dict[str, list[tuple[str, float]]] = {}
    for trade, symbol in trade_rows:
        key = trade.trade_date.strftime("%Y-%m-%d")
        trades_by_date.setdefault(key, []).append((symbol, float(trade.quantity)))

    # ── Simulate forward through dates ────────────────────────────────────
    qty: dict[str, float] = dict(starting_qty)
    history: list[dict] = []

    for ts in close_df.index:
        # pandas Timestamp → datetime
        date_str = ts.strftime("%Y-%m-%d")

        # Apply trades for this day
        for sym, delta in trades_by_date.get(date_str, []):
            qty[sym] = qty.get(sym, 0.0) + delta

        # Compute portfolio value
        total = 0.0
        for symbol in all_symbols:
            q = qty.get(symbol, 0.0)
            if q == 0.0:
                continue
            try:
                price = float(close_df.loc[ts, symbol])
                if price > 0:
                    total += q * price
            except (KeyError, TypeError):
                pass

        # Skip dates where the portfolio is effectively zero (before any positions)
        if total > 0.0:
            history.append({"date": date_str, "value": round(total, 2)})

    return history
