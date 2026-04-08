"""Fetch live and historical FX rates from Yahoo Finance (primary)
and the ECB/Frankfurter API (fallback).

Yahoo Finance tickers use the format "EURUSD=X" for EUR→USD.
The Frankfurter API (https://api.frankfurter.dev) is free, no key needed,
and sourced from the European Central Bank.
"""
import logging
from datetime import datetime, timedelta

import httpx
import yfinance as yf
from sqlalchemy.orm import Session

from app.services.fx_service import COMMON_CURRENCIES, upsert_rate

log = logging.getLogger(__name__)

FRANKFURTER_BASE = "https://api.frankfurter.dev"


def fetch_yahoo_current(
    base: str,
    quotes: list[str] | None = None,
) -> dict[str, float]:
    """Fetch current rates from Yahoo Finance.

    Returns dict of {quote_currency: rate} where rate is units of
    quote per 1 unit of base.
    """
    if quotes is None:
        quotes = [c for c in COMMON_CURRENCIES if c != base]

    tickers = [f"{base}{q}=X" for q in quotes]
    rates: dict[str, float] = {}

    try:
        data = yf.download(
            tickers,
            period="1d",
            progress=False,
            auto_adjust=True,
        )

        if data.empty:
            log.warning("Yahoo Finance returned empty data")
            return rates

        close_data = data["Close"]
        for quote, ticker in zip(quotes, tickers):
            try:
                if len(tickers) == 1:
                    col = close_data.iloc[:, 0] if hasattr(close_data, "columns") else close_data
                else:
                    col = close_data[ticker]
                val = col.iloc[-1]
                close = float(val)
                if close and close > 0:
                    rates[quote] = round(close, 6)
            except (KeyError, IndexError, TypeError, ValueError):
                continue

    except Exception as e:
        log.error("Yahoo Finance fetch failed: %s", e)

    return rates


def fetch_yahoo_historical(
    base: str,
    quote: str,
    start_date: datetime,
    end_date: datetime | None = None,
) -> list[dict]:
    """Fetch historical daily rates from Yahoo Finance.

    Returns list of {date: datetime, rate: float}.
    """
    if end_date is None:
        end_date = datetime.now()

    ticker = f"{base}{quote}=X"
    results: list[dict] = []

    try:
        data = yf.download(
            ticker,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )

        if data.empty:
            return results

        close_col = data["Close"]
        if hasattr(close_col, "columns"):
            close_col = close_col.iloc[:, 0]

        for idx in close_col.index:
            val = close_col.loc[idx]
            try:
                close = float(val)
            except (TypeError, ValueError):
                continue
            if close > 0:
                dt = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                results.append({
                    "date": dt.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ),
                    "rate": round(close, 6),
                })

    except Exception as e:
        log.error("Yahoo historical fetch failed for %s: %s", ticker, e)

    return results


def fetch_frankfurter_current(
    base: str,
    quotes: list[str] | None = None,
) -> dict[str, float]:
    """Fallback: fetch current rates from the ECB via Frankfurter API."""
    if quotes is None:
        quotes = [c for c in COMMON_CURRENCIES if c != base]

    try:
        resp = httpx.get(
            f"{FRANKFURTER_BASE}/latest",
            params={"base": base, "symbols": ",".join(quotes)},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {k: round(v, 6) for k, v in data.get("rates", {}).items()}
    except Exception as e:
        log.error("Frankfurter API fetch failed: %s", e)
        return {}


def fetch_frankfurter_historical(
    base: str,
    quote: str,
    start_date: datetime,
    end_date: datetime | None = None,
) -> list[dict]:
    """Fallback: fetch historical rates from the ECB via Frankfurter API.

    Splits large ranges into yearly chunks to avoid 404s on the API.
    """
    if end_date is None:
        end_date = datetime.now()

    results: list[dict] = []
    chunk_start = start_date

    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=365), end_date)
        try:
            resp = httpx.get(
                f"{FRANKFURTER_BASE}/{chunk_start.strftime('%Y-%m-%d')}"
                f"..{chunk_end.strftime('%Y-%m-%d')}",
                params={"base": base, "symbols": quote},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            for date_str, rates in data.get("rates", {}).items():
                rate = rates.get(quote)
                if rate and rate > 0:
                    results.append({
                        "date": datetime.strptime(date_str, "%Y-%m-%d"),
                        "rate": round(rate, 6),
                    })
        except Exception as e:
            log.warning(
                "Frankfurter chunk %s..%s failed: %s",
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                e,
            )

        chunk_start = chunk_end + timedelta(days=1)

    return sorted(results, key=lambda r: r["date"])


def sync_current_rates(
    db: Session,
    base: str = "USD",
    quotes: list[str] | None = None,
) -> dict[str, str]:
    """Fetch today's rates and store them. Tries Yahoo first, then ECB.

    Returns {currency: "status"} for each quote currency.
    """
    if quotes is None:
        quotes = [c for c in COMMON_CURRENCIES if c != base]

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    statuses: dict[str, str] = {}

    # Try Yahoo Finance first
    yahoo_rates = fetch_yahoo_current(base, quotes)
    for ccy, rate in yahoo_rates.items():
        upsert_rate(db, base, ccy, today, rate, source="yahoo_finance")
        statuses[ccy] = "yahoo_finance"
        log.info("Stored %s/%s = %.6f from Yahoo", base, ccy, rate)

    # Fallback to Frankfurter for any missing
    missing = [q for q in quotes if q not in statuses]
    if missing:
        frank_rates = fetch_frankfurter_current(base, missing)
        for ccy, rate in frank_rates.items():
            upsert_rate(db, base, ccy, today, rate, source="ecb_frankfurter")
            statuses[ccy] = "ecb_frankfurter"
            log.info("Stored %s/%s = %.6f from ECB", base, ccy, rate)

    for ccy in quotes:
        if ccy not in statuses:
            statuses[ccy] = "failed"

    return statuses


def sync_historical_rates(
    db: Session,
    base: str,
    quote: str,
    start_date: datetime,
    end_date: datetime | None = None,
) -> int:
    """Fetch and store historical daily rates for a single pair.

    Returns number of rates stored.
    """
    # Try Yahoo first
    rates = fetch_yahoo_historical(base, quote, start_date, end_date)
    source = "yahoo_finance"

    # Fallback to Frankfurter
    if not rates:
        rates = fetch_frankfurter_historical(base, quote, start_date, end_date)
        source = "ecb_frankfurter"

    count = 0
    for entry in rates:
        upsert_rate(db, base, quote, entry["date"], entry["rate"], source=source)
        count += 1

    log.info(
        "Synced %d historical rates for %s/%s from %s",
        count, base, quote, source,
    )
    return count
