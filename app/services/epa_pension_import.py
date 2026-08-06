"""WTW ePA "My Fund Balance" pension statement import and valuation.

The statement lists each fund with its unit holding and the scheme's own unit
price (these funds are not exchange-listed, so there is no live market feed —
the unit price *is* the price of record). We store one Instrument + PositionLot
+ PriceSnapshot per fund and value the account as Σ units × latest unit price.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.clock import naive_utc_now

log = logging.getLogger(__name__)

# Detail row: units  unit_price  dd/mm/yyyy  value  £value  percentage
_ROW_RE = re.compile(
    r"([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+(\d{2}/\d{2}/\d{4})\s+"
    r"([\d,]+\.\d{2})\s+£([\d,]+\.\d{2})\s+(\d+\.\d+)"
)
# Summary bullet: "u <fund name> £<value>"
_SUMMARY_RE = re.compile(r"^\s*u\s+(.+?)\s+£([\d,]+\.\d{2})\s*$", re.MULTILINE)
_TOTAL_RE = re.compile(r"Total Value:\s*£([\d,]+\.\d{2})")


def _dec(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()
    return f"EPA:{s}"


@dataclass
class PensionFund:
    name: str
    units: Decimal
    unit_price: Decimal
    price_date: date | None
    value: Decimal
    percentage: Decimal | None = None


@dataclass
class ParsedPension:
    total_value: Decimal | None
    currency: str
    funds: list[PensionFund] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return bool(self.funds)


def is_epa_pension_pdf(filepath: str) -> bool:
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            text = (pdf.pages[0].extract_text() or "").lower() if pdf.pages else ""
    except Exception:
        return False
    return "balance by fund" in text and "total value" in text and (
        "my-savingsmy-pension" in text or "epa" in text or "unit price" in text
    )


def parse_epa_pension_pdf(filepath: str) -> ParsedPension:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return parse_epa_pension_text("\n".join(parts))


def parse_epa_pension_text(text: str) -> ParsedPension:
    total_m = _TOTAL_RE.search(text)
    total_value = _dec(total_m.group(1)) if total_m else None

    # Clean fund names keyed by their £value (values are distinct on a statement).
    names_by_value: dict[Decimal, str] = {}
    for m in _SUMMARY_RE.finditer(text):
        names_by_value[_dec(m.group(2))] = m.group(1).strip()

    funds: list[PensionFund] = []
    for m in _ROW_RE.finditer(text):
        units = _dec(m.group(1))
        unit_price = _dec(m.group(2))
        try:
            pdate = datetime.strptime(m.group(3), "%d/%m/%Y").date()
        except ValueError:
            pdate = None
        value = _dec(m.group(4))
        pct = _dec(m.group(6))
        name = names_by_value.get(value) or f"Fund {len(funds) + 1}"
        funds.append(PensionFund(
            name=name, units=units, unit_price=unit_price,
            price_date=pdate, value=value, percentage=pct,
        ))

    return ParsedPension(total_value=total_value, currency="GBP", funds=funds)


def _get_or_create_instrument(db: Session, symbol: str, name: str, currency: str):
    from app.models.instrument import Instrument

    inst = db.execute(
        select(Instrument).where(Instrument.symbol == symbol).limit(1)
    ).scalar_one_or_none()
    if inst is None:
        inst = Instrument(
            symbol=symbol, name=name, currency=currency, asset_class="pension_fund",
        )
        db.add(inst)
        db.flush()
    return inst


def import_pension_positions(db: Session, account, parsed: ParsedPension) -> dict:
    """Persist one position lot + unit-price snapshot per fund; value the account.

    Idempotent: replaces this account's prior ePA position lots so re-importing
    a newer statement refreshes holdings instead of stacking them.
    """
    from app.models.instrument import PositionLot, PriceSnapshot

    if not parsed.is_valid:
        raise ValueError("Not a recognisable ePA pension statement (no funds found).")

    currency = "GBP"
    as_of = naive_utc_now()

    # Clear prior ePA lots for this account (fresh statement snapshot).
    old = db.execute(
        select(PositionLot).where(
            PositionLot.account_id == account.id,
            PositionLot.source == "epa_pension",
        )
    ).scalars().all()
    for lot in old:
        db.delete(lot)
    db.flush()

    for f in parsed.funds:
        inst = _get_or_create_instrument(db, _slug(f.name), f.name, currency)
        price_dt = datetime.combine(f.price_date or as_of.date(), datetime.min.time())
        # The statement's displayed unit price is rounded (e.g. 12.34), so
        # units × displayed_price doesn't reconcile to the reported fund value.
        # Store the precise implied price (value ÷ units) so valuation matches
        # the statement to the penny; the displayed price is kept in notes.
        if f.units and f.units != 0:
            precise_price = (f.value / f.units).quantize(Decimal("0.000001"))
        else:
            precise_price = f.unit_price
        db.add(PositionLot(
            account_id=account.id,
            instrument_id=inst.id,
            quantity=float(f.units),
            as_of_date=price_dt,
            source="epa_pension",
            confidence=0.95,
            notes=f"Statement unit price {f.unit_price} {currency} on {f.price_date}",
        ))
        db.add(PriceSnapshot(
            instrument_id=inst.id,
            as_of_date=price_dt,
            price=precise_price,
            currency=currency,
            source="epa_statement",
            confidence=0.95,
            stale_flag=False,
        ))
    db.flush()

    from app.models.enums import BalanceTruthSource
    account.balance_truth_source = BalanceTruthSource.LATEST_VALUATION.value
    if account.currency is None:
        account.currency = currency
    val = value_pension_account(db, account)
    db.commit()

    return {"funds": len(parsed.funds), "valuation": val}


def value_pension_account(db: Session, account, persist: bool = True) -> dict:
    """Value = Σ (latest units × latest unit price) across the account's funds.

    Writes an AssetValuation when ``persist`` is set; pass False for read-only
    page views so a valuation row isn't created on every GET.
    """
    from app.models.asset_valuation import AssetValuation
    from app.models.instrument import Instrument, PositionLot, PriceSnapshot

    lots = db.execute(
        select(PositionLot, Instrument)
        .join(Instrument, PositionLot.instrument_id == Instrument.id)
        .where(
            PositionLot.account_id == account.id,
            PositionLot.source == "epa_pension",
        )
    ).all()

    currency = account.currency or "GBP"
    total = Decimal("0")
    holdings: list[dict] = []
    as_of = naive_utc_now()
    for lot, inst in lots:
        snap = db.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.instrument_id == inst.id)
            .order_by(PriceSnapshot.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is None:
            continue
        price = Decimal(snap.price)
        units = Decimal(str(lot.quantity))
        value = (units * price).quantize(Decimal("0.01"))
        total += value
        holdings.append({
            "name": inst.name, "units": units, "unit_price": price,
            "value": value, "as_of": snap.as_of_date,
        })
        as_of = max(as_of, snap.as_of_date) if snap.as_of_date else as_of

    total = total.quantize(Decimal("0.01"))
    if holdings and persist:
        db.add(AssetValuation(
            account_id=account.id,
            date=naive_utc_now(),
            value=total,
            currency=currency,
            source="epa_pension",
            notes=f"{len(holdings)} funds × unit price",
        ))
        db.flush()

    return {"currency": currency, "value": total, "holdings": holdings}
