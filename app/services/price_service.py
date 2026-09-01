"""Price service — fetch current prices and reconstruct portfolio history.

Uses yfinance for live prices and historical data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from app.services.clock import naive_utc_now

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def get_current_prices(
    symbols: list[str],
    db: "Session | None" = None,
) -> tuple[dict[str, float], dict[str, datetime], bool]:
    """Fetch the latest price for each symbol using yfinance fast_info.

    Falls back to the most recent PriceSnapshot in the DB for any symbol
    that cannot be fetched live.

    Returns:
        prices  — {symbol: float}
        as_of   — {symbol: datetime} when each price was fetched or last cached
        live    — True if at least one price came from a live fetch
    """
    prices: dict[str, float] = {}
    as_of: dict[str, datetime] = {}
    live = False
    now = naive_utc_now()

    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            price = ticker.fast_info.last_price
            if price is not None and price > 0:
                prices[symbol] = float(price)
                as_of[symbol] = now
                live = True
                if db is not None:
                    _save_price_snapshot(db, symbol, float(price), now)
            else:
                log.warning("price_service: no price for %s (got %r)", symbol, price)
        except Exception as exc:
            log.warning("price_service: failed to fetch price for %s: %s", symbol, exc)

    # Fall back to DB cache for any symbols we didn't get live
    missing = [s for s in symbols if s not in prices]
    if missing and db is not None:
        for sym, (price, ts) in _load_cached_prices(db, missing).items():
            prices[sym] = price
            as_of[sym] = ts

    return prices, as_of, live


def _save_price_snapshot(db: "Session", symbol: str, price: float, ts: datetime) -> None:
    """Upsert today's live price into price_snapshots."""
    from app.models.instrument import Instrument, PriceSnapshot

    inst = db.execute(
        select(Instrument).where(Instrument.symbol == symbol).limit(1)
    ).scalar_one_or_none()
    if inst is None:
        return

    today_start = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    existing = db.execute(
        select(PriceSnapshot).where(
            PriceSnapshot.instrument_id == inst.id,
            PriceSnapshot.as_of_date >= today_start,
        ).limit(1)
    ).scalar_one_or_none()

    if existing is not None:
        existing.price = price
        existing.as_of_date = ts
        existing.stale_flag = False
    else:
        db.add(PriceSnapshot(
            instrument_id=inst.id,
            as_of_date=ts,
            price=price,
            currency=inst.currency or "USD",
            source="yfinance_live",
            confidence=0.95,
            stale_flag=False,
        ))
    # Flush only — let the caller own the transaction boundary. Committing
    # here would prematurely persist any unrelated writes the caller has
    # staged on this session.
    try:
        db.flush()
    except Exception as exc:
        log.warning("price_service: failed to flush snapshot for %s: %s", symbol, exc)


def _load_cached_prices(
    db: "Session", symbols: list[str]
) -> dict[str, tuple[float, datetime]]:
    """Return most recent PriceSnapshot for each symbol."""
    from app.models.instrument import Instrument, PriceSnapshot

    result: dict[str, tuple[float, datetime]] = {}
    for symbol in symbols:
        row = db.execute(
            select(PriceSnapshot)
            .join(Instrument, PriceSnapshot.instrument_id == Instrument.id)
            .where(Instrument.symbol == symbol)
            .order_by(PriceSnapshot.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is not None and row.price:
            result[symbol] = (float(row.price), row.as_of_date)
    return result


def compute_account_value_by_symbol(db: Session, account_id: int) -> dict:
    """Daily market value of an account split per holding, plus dividends.

    Reconstructs each symbol's quantity backwards from today's position lots
    less the trade history, then walks forward applying trades and pricing
    each day's holding at that day's close.

    Returns ``{"dates": [...], "series": {symbol: [...]},
    "dividends_cum": [...], "total": [...]}``. Cash is deliberately absent —
    the IBKR importer does not record a cash balance, so any cash line here
    would be invented.
    """
    from app.models.instrument import Instrument, PositionLot
    from app.models.stock_dividend import StockDividend
    from app.models.stock_trade import StockTrade

    trade_rows = db.execute(
        select(StockTrade, Instrument.symbol)
        .join(Instrument, StockTrade.instrument_id == Instrument.id)
        .where(StockTrade.account_id == account_id)
        .order_by(StockTrade.trade_date)
    ).all()
    position_rows = db.execute(
        select(PositionLot, Instrument.symbol)
        .join(Instrument, PositionLot.instrument_id == Instrument.id)
        .where(PositionLot.account_id == account_id)
    ).all()
    if not trade_rows and not position_rows:
        return {"dates": [], "series": {}, "dividends_cum": [], "total": []}

    current_qty: dict[str, float] = {}
    for lot, symbol in position_rows:
        current_qty[symbol] = current_qty.get(symbol, 0.0) + float(lot.quantity)

    all_symbols = set(current_qty) | {s for _, s in trade_rows}
    if not all_symbols:
        return {"dates": [], "series": {}, "dividends_cum": [], "total": []}

    start_date = (
        min(t.trade_date for t, _ in trade_rows) if trade_rows
        else naive_utc_now() - timedelta(days=365)
    ) - timedelta(days=5)

    symbols_list = sorted(all_symbols)
    try:
        raw = yf.download(
            symbols_list, start=start_date.strftime("%Y-%m-%d"),
            auto_adjust=True, progress=False,
        )
    except Exception as exc:
        log.warning("price_service: yfinance download failed: %s", exc)
        return {"dates": [], "series": {}, "dividends_cum": [], "total": []}
    if raw is None or raw.empty:
        return {"dates": [], "series": {}, "dividends_cum": [], "total": []}

    try:
        if hasattr(raw.columns, "levels"):
            close_df = raw["Close"]
        else:
            close_df = raw[["Close"]]
            close_df.columns = symbols_list
        close_df = close_df.ffill()
    except Exception as exc:
        log.warning("price_service: price frame processing failed: %s", exc)
        return {"dates": [], "series": {}, "dividends_cum": [], "total": []}

    # Wind quantities back to the start of the window.
    trade_sum: dict[str, float] = {}
    for trade, symbol in trade_rows:
        trade_sum[symbol] = trade_sum.get(symbol, 0.0) + float(trade.quantity)
    qty = {s: current_qty.get(s, 0.0) - trade_sum.get(s, 0.0) for s in all_symbols}

    trades_by_date: dict[str, list[tuple[str, float]]] = {}
    for trade, symbol in trade_rows:
        trades_by_date.setdefault(
            trade.trade_date.strftime("%Y-%m-%d"), []
        ).append((symbol, float(trade.quantity)))

    # Kept as a sorted list rather than a date→amount map: dividends paid on a
    # weekend or market holiday have no matching row in the price index, and an
    # exact-date lookup would silently drop them from the running total.
    div_events = sorted(
        (d.pay_date.strftime("%Y-%m-%d"), float(d.amount_native))
        for d in db.execute(
            select(StockDividend).where(StockDividend.account_id == account_id)
        ).scalars().all()
    )
    div_idx = 0

    dates: list[str] = []
    series: dict[str, list[float]] = {s: [] for s in symbols_list}
    dividends_cum: list[float] = []
    totals: list[float] = []
    running_div = 0.0

    for ts in close_df.index:
        date_str = ts.strftime("%Y-%m-%d")
        for sym, delta in trades_by_date.get(date_str, []):
            qty[sym] = qty.get(sym, 0.0) + delta
        # Sweep in every dividend paid on or before this date.
        while div_idx < len(div_events) and div_events[div_idx][0] <= date_str:
            running_div += div_events[div_idx][1]
            div_idx += 1

        day_total = 0.0
        day_vals: dict[str, float] = {}
        for symbol in symbols_list:
            q = qty.get(symbol, 0.0)
            val = 0.0
            if q:
                try:
                    price = float(close_df.loc[ts, symbol])
                    if price > 0:
                        val = round(q * price, 2)
                except (KeyError, TypeError, ValueError):
                    val = 0.0
            day_vals[symbol] = val
            day_total += val

        # Skip the run-up before the account holds anything.
        if day_total <= 0 and not dates:
            continue
        dates.append(date_str)
        for symbol in symbols_list:
            series[symbol].append(day_vals[symbol])
        dividends_cum.append(round(running_div, 2))
        totals.append(round(day_total, 2))

    # Drop symbols that never held value in the window.
    series = {s: v for s, v in series.items() if any(v)}
    return {
        "dates": dates, "series": series,
        "dividends_cum": dividends_cum, "total": totals,
    }


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
        start_date = naive_utc_now() - timedelta(days=365)

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
