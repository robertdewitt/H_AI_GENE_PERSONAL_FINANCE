"""IBKR Activity Statement CSV parser and DB importer.

IBKR flex / activity CSVs are multi-section files where every row starts with:
    SectionName, RowType, col1, col2, ...

Supported sections:
  Statement, Account Information, Net Asset Value,
  Financial Instrument Information, Open Positions,
  Mark-to-Market Performance Summary, Trades, Dividends
"""
from __future__ import annotations

import csv
import logging
import re
from collections import defaultdict
from datetime import datetime
from app.services.clock import naive_utc_now
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(value: str | None, fallback: float = 0.0) -> float:
    if value is None:
        return fallback
    v = value.strip().replace(",", "")
    if v in ("", "--", "-"):
        return fallback
    try:
        return float(v)
    except ValueError:
        return fallback


def _to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    v = value.strip().replace(",", "")
    if v in ("", "--", "-"):
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def _parse_period(raw: str) -> tuple[datetime | None, datetime | None]:
    """Parse IBKR period strings like 'May 30, 2025 - May 8, 2026'."""
    raw = raw.strip()
    # Try range first
    parts = re.split(r"\s*-\s*(?=[A-Za-z])", raw, maxsplit=1)
    fmts = ["%B %d, %Y", "%Y-%m-%d"]
    def _try_parse(s: str) -> datetime | None:
        s = s.strip()
        for fmt in fmts:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None

    if len(parts) == 2:
        return _try_parse(parts[0]), _try_parse(parts[1])
    single = _try_parse(raw)
    return single, single


def _parse_section(
    section_rows: list[list[str]],
    data_row_types: tuple[str, ...] = ("Data",),
) -> list[dict[str, str]]:
    """Extract a header + data rows from one section's raw CSV rows.

    Each raw row is a list of strings already split by the CSV reader.
    row[0] = section name, row[1] = RowType, row[2:] = payload columns.

    Returns a list of dicts keyed by the header columns.
    """
    headers: list[str] = []
    results: list[dict[str, str]] = []

    for row in section_rows:
        if len(row) < 3:
            continue
        row_type = row[1].strip()
        if row_type == "Header":
            headers = [c.strip() for c in row[2:]]
        elif row_type in data_row_types and headers:
            payload = row[2:]
            record: dict[str, str] = {}
            for i, col in enumerate(headers):
                record[col] = payload[i].strip() if i < len(payload) else ""
            results.append(record)

    return results


# ---------------------------------------------------------------------------
# Public: is_ibkr_file
# ---------------------------------------------------------------------------

def is_ibkr_file(filepath: str) -> bool:
    """Return True if the file looks like an IBKR activity statement CSV."""
    try:
        with open(filepath, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if not row or all(c.strip() == "" for c in row):
                    continue
                # First meaningful row must start with "Statement" and contain "Field Name"
                return (
                    len(row) >= 3
                    and row[0].strip() == "Statement"
                    and "Field Name" in row
                )
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public: parse_ibkr_csv
# ---------------------------------------------------------------------------

def parse_ibkr_csv(filepath: str) -> dict[str, Any]:
    """Parse an IBKR activity statement CSV and return a structured dict."""

    # Group raw rows by section name
    sections: dict[str, list[list[str]]] = defaultdict(list)

    with open(filepath, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row:
                continue
            section = row[0].strip()
            if section:
                sections[section].append(row)

    # ── Statement metadata ────────────────────────────────────────────────
    base_currency = "USD"
    account_ibkr = ""
    period_start: datetime | None = None
    period_end: datetime | None = None

    for row in sections.get("Statement", []):
        if len(row) < 4:
            continue
        key = row[2].strip()
        val = row[3].strip()
        if key == "Period":
            period_start, period_end = _parse_period(val)
        if key == "Base Currency" or key == "Base Cur.":
            base_currency = val

    for row in sections.get("Account Information", []):
        if len(row) < 4:
            continue
        key = row[2].strip()
        val = row[3].strip()
        if key == "Base Currency":
            base_currency = val
        if key == "Account":
            account_ibkr = val

    # ── NAV ───────────────────────────────────────────────────────────────
    nav_total: float = 0.0
    nav_rows = _parse_section(sections.get("Net Asset Value", []))
    for r in nav_rows:
        # Find the Total row
        asset_class = r.get("Asset Class", "").strip()
        if asset_class.lower() == "total":
            nav_total = _to_float(r.get("Current Total"))
            break

    # ── Financial Instrument Information ──────────────────────────────────
    instruments: list[dict[str, str]] = []
    for r in _parse_section(sections.get("Financial Instrument Information", [])):
        symbol = r.get("Symbol", "").strip()
        if not symbol:
            continue
        instruments.append({
            "symbol": symbol,
            "name": r.get("Description", "").strip(),
            "isin": r.get("Security ID", "").strip(),
            "exchange": r.get("Listing Exch", "").strip(),
            "currency": r.get("Currency", "USD").strip() or "USD",
        })

    # ── Open Positions ────────────────────────────────────────────────────
    positions: list[dict] = []
    # Only "Summary" DataDiscriminator rows
    all_position_rows = _parse_section(sections.get("Open Positions", []))
    for r in all_position_rows:
        discriminator = r.get("DataDiscriminator", "").strip()
        if discriminator != "Summary":
            continue
        symbol = r.get("Symbol", "").strip()
        if not symbol:
            continue
        positions.append({
            "symbol": symbol,
            "currency": r.get("Currency", "USD").strip() or "USD",
            "quantity": _to_float(r.get("Quantity")),
            "cost_price": _to_float(r.get("Cost Price")),
            "cost_basis": _to_float(r.get("Cost Basis")),
            "close_price": _to_float(r.get("Close Price")),
            "value": _to_float(r.get("Value")),
            "unrealized_pnl": _to_float(r.get("Unrealized P/L")),
        })

    # ── Mark-to-Market Performance Summary ───────────────────────────────
    prior_positions: list[dict] = []
    for r in _parse_section(sections.get("Mark-to-Market Performance Summary", [])):
        symbol = r.get("Symbol", "").strip()
        if not symbol:
            continue
        prior_qty = _to_float(r.get("Prior Quantity"))
        prior_price_raw = r.get("Prior Price", "").strip()
        prior_price = _to_float(prior_price_raw) if prior_price_raw not in ("--", "") else 0.0
        prior_positions.append({
            "symbol": symbol,
            "prior_qty": prior_qty,
            "prior_price": prior_price,
        })

    # ── Trades ────────────────────────────────────────────────────────────
    trades: list[dict] = []
    for r in _parse_section(sections.get("Trades", [])):
        discriminator = r.get("DataDiscriminator", "").strip()
        if discriminator != "Order":
            continue
        asset_cat = r.get("Asset Category", "").strip()
        if "Stocks" not in asset_cat and "Equity" not in asset_cat:
            continue
        symbol = r.get("Symbol", "").strip()
        if not symbol:
            continue

        # Date/Time can be quoted with comma: "2025-11-04, 09:30:00"
        date_raw = r.get("Date/Time", "").strip().strip('"')
        trade_date: datetime | None = None
        for fmt in ("%Y-%m-%d, %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                trade_date = datetime.strptime(date_raw, fmt)
                break
            except ValueError:
                pass

        if trade_date is None:
            log.warning("ibkr_import: could not parse trade date %r for %s", date_raw, symbol)
            continue

        trades.append({
            "symbol": symbol,
            "currency": r.get("Currency", "USD").strip() or "USD",
            "trade_date": trade_date,
            "quantity": _to_float(r.get("Quantity")),
            "price": _to_float(r.get("T. Price")),
            "proceeds": _to_float(r.get("Proceeds")),
            "commission": _to_float(r.get("Comm/Fee")),
            "realized_pnl": _to_float(r.get("Realized P/L")),
        })

    # ── Dividends ─────────────────────────────────────────────────────────
    dividends: list[dict] = []
    for r in _parse_section(sections.get("Dividends", [])):
        description = r.get("Description", "").strip()
        # Skip subtotal / total rows
        if not description or description.lower().startswith("total"):
            continue
        # Extract symbol from "SYMBOL(ISIN) ..."
        symbol_match = re.match(r"^([A-Z0-9.]+)\(", description)
        symbol = symbol_match.group(1) if symbol_match else ""
        if not symbol:
            continue

        date_raw = r.get("Date", "").strip()
        pay_date: datetime | None = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                pay_date = datetime.strptime(date_raw, fmt)
                break
            except ValueError:
                pass
        if pay_date is None:
            log.warning("ibkr_import: could not parse dividend date %r", date_raw)
            continue

        dividends.append({
            "symbol": symbol,
            "currency": r.get("Currency", "USD").strip() or "USD",
            "pay_date": pay_date,
            "amount": _to_float(r.get("Amount")),
            "description": description,
        })

    return {
        "base_currency": base_currency,
        "account_ibkr": account_ibkr,
        "period_start": period_start,
        "period_end": period_end,
        "nav_total": nav_total,
        "instruments": instruments,
        "positions": positions,
        "prior_positions": prior_positions,
        "trades": trades,
        "dividends": dividends,
    }


# ---------------------------------------------------------------------------
# Public: apply_ibkr_statement
# ---------------------------------------------------------------------------

def apply_ibkr_statement(db: Session, account_id: int, parsed: dict) -> dict[str, int]:
    """Persist a parsed IBKR statement to the database.

    Returns a stats dict with keys:
        instruments_upserted, positions_updated, trades_added,
        dividends_added, nav_saved
    """
    from sqlalchemy import select

    from app.models.account import Account
    from app.models.asset_valuation import AssetValuation
    from app.models.instrument import Instrument, PositionLot, PriceSnapshot
    from app.models.stock_dividend import StockDividend
    from app.models.stock_trade import StockTrade

    stats = {
        "instruments_upserted": 0,
        "positions_updated": 0,
        "trades_added": 0,
        "dividends_added": 0,
        "nav_saved": 0,
    }

    as_of_date = parsed["period_end"] or naive_utc_now()
    base_currency = parsed["base_currency"]

    # ── 1. Upsert instruments ─────────────────────────────────────────────
    symbol_to_instrument: dict[str, Instrument] = {}

    # Pre-load all referenced symbols in one query
    all_symbols = {r["symbol"] for r in parsed["instruments"]}
    all_symbols.update(r["symbol"] for r in parsed["positions"])
    all_symbols.update(r["symbol"] for r in parsed["trades"])
    all_symbols.update(r["symbol"] for r in parsed["dividends"])
    all_symbols.discard("")

    existing = db.execute(
        select(Instrument).where(Instrument.symbol.in_(all_symbols))
    ).scalars().all()
    for inst in existing:
        symbol_to_instrument[inst.symbol] = inst

    # Build a lookup from the instruments section for enrichment
    inst_info: dict[str, dict] = {r["symbol"]: r for r in parsed["instruments"]}

    for symbol in all_symbols:
        info = inst_info.get(symbol, {})
        if symbol in symbol_to_instrument:
            inst = symbol_to_instrument[symbol]
            # Update name if we have a better one
            if info.get("name") and not inst.name:
                inst.name = info["name"]
            if info.get("isin") and not inst.cusip:
                inst.cusip = info["isin"]
        else:
            inst = Instrument(
                symbol=symbol,
                name=info.get("name") or symbol,
                currency=info.get("currency", "USD"),
                asset_class="Stocks",
                cusip=info.get("isin") or None,
            )
            db.add(inst)
            db.flush()  # populate inst.id
            symbol_to_instrument[symbol] = inst
        stats["instruments_upserted"] += 1

    # ── 2. Replace position lots for this account (ibkr source) ──────────
    existing_lots = db.execute(
        select(PositionLot).where(
            PositionLot.account_id == account_id,
            PositionLot.source == "ibkr",
        )
    ).scalars().all()
    for lot in existing_lots:
        db.delete(lot)
    db.flush()

    for pos in parsed["positions"]:
        symbol = pos["symbol"]
        inst = symbol_to_instrument.get(symbol)
        if inst is None:
            continue
        lot = PositionLot(
            account_id=account_id,
            instrument_id=inst.id,
            quantity=pos["quantity"],
            cost_basis_total=Decimal(str(pos["cost_basis"])) if pos["cost_basis"] else None,
            as_of_date=as_of_date,
            source="ibkr",
            confidence=0.95,
            notes=f"Imported from IBKR statement as of {as_of_date.date()}",
        )
        db.add(lot)
        stats["positions_updated"] += 1

    # ── 3. Price snapshots ────────────────────────────────────────────────
    as_of_day = as_of_date.date()
    for pos in parsed["positions"]:
        symbol = pos["symbol"]
        close_price = pos["close_price"]
        if close_price == 0.0:
            continue
        inst = symbol_to_instrument.get(symbol)
        if inst is None:
            continue

        # Skip if already have a snapshot for this instrument + day
        existing_snap = db.execute(
            select(PriceSnapshot).where(
                PriceSnapshot.instrument_id == inst.id,
                PriceSnapshot.as_of_date >= datetime(as_of_day.year, as_of_day.month, as_of_day.day),
                PriceSnapshot.as_of_date < datetime(as_of_day.year, as_of_day.month, as_of_day.day, 23, 59, 59),
            ).limit(1)
        ).scalar_one_or_none()

        if existing_snap is None:
            snap = PriceSnapshot(
                instrument_id=inst.id,
                as_of_date=as_of_date,
                price=Decimal(str(close_price)),
                currency=pos.get("currency", base_currency),
                source="ibkr_statement",
                confidence=0.95,
            )
            db.add(snap)

    # ── 4. Trades ─────────────────────────────────────────────────────────
    for trade in parsed["trades"]:
        symbol = trade["symbol"]
        inst = symbol_to_instrument.get(symbol)
        if inst is None:
            continue

        qty = trade["quantity"]
        price = trade["price"]
        trade_date = trade["trade_date"]

        dedup_key = (
            f"{account_id}:{symbol}:{trade_date.isoformat()}:{qty}:{price}"
        )

        # Check dedup
        existing_trade = db.execute(
            select(StockTrade).where(
                StockTrade.ibkr_dedup_key == dedup_key,
            ).limit(1)
        ).scalar_one_or_none()

        if existing_trade is not None:
            continue

        st = StockTrade(
            account_id=account_id,
            instrument_id=inst.id,
            trade_date=trade_date,
            quantity=qty,
            price=Decimal(str(price)),
            proceeds=Decimal(str(trade["proceeds"])) if trade["proceeds"] else None,
            commission=Decimal(str(trade["commission"])) if trade["commission"] else None,
            realized_pnl=Decimal(str(trade["realized_pnl"])) if trade["realized_pnl"] else None,
            currency=trade.get("currency", "USD"),
            source="ibkr",
            ibkr_dedup_key=dedup_key,
        )
        db.add(st)
        stats["trades_added"] += 1

    # ── 5. Dividends ──────────────────────────────────────────────────────
    for div in parsed["dividends"]:
        symbol = div["symbol"]
        inst = symbol_to_instrument.get(symbol)
        if inst is None:
            continue

        pay_date = div["pay_date"]
        amount = Decimal(str(div["amount"]))

        # Dedup by (account_id, instrument_id, pay_date, amount_native)
        existing_div = db.execute(
            select(StockDividend).where(
                StockDividend.account_id == account_id,
                StockDividend.instrument_id == inst.id,
                StockDividend.pay_date == pay_date,
                StockDividend.amount_native == amount,
            ).limit(1)
        ).scalar_one_or_none()

        if existing_div is not None:
            continue

        sd = StockDividend(
            account_id=account_id,
            instrument_id=inst.id,
            pay_date=pay_date,
            amount_native=amount,
            currency=div.get("currency", "USD"),
            description=div.get("description"),
            source="ibkr",
        )
        db.add(sd)
        stats["dividends_added"] += 1

    # ── 6. NAV as AssetValuation ──────────────────────────────────────────
    nav = parsed.get("nav_total", 0.0)
    if nav and nav != 0.0:
        av = AssetValuation(
            account_id=account_id,
            date=as_of_date,
            value=Decimal(str(nav)),
            currency=base_currency,
            source="ibkr_statement",
            notes=f"NAV from IBKR statement period ending {as_of_date.date()}",
        )
        db.add(av)
        stats["nav_saved"] = 1

        # Switch the account to use valuation-based balance so the NAV
        # shows up on the accounts and net worth pages instead of summing
        # transactions (which would be 0 for a pure brokerage account).
        account = db.get(Account, account_id)
        if account is not None:
            account.balance_truth_source = "latest_valuation"

    db.commit()
    return stats
