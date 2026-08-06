"""Merrill / Bank of America RSU award-summary import and valuation.

Parses the "Your Awards" CSV export into grants + vesting tranches, stores
them against an RSU account, and values the account at unvested units × the
live market price of the underlying stock.

The vested units in these exports are already delivered out of the plan (the
linked brokerage shows £0 / $0 of shares), so counting them would double-count
against a brokerage holding. The account is therefore valued on the *unvested*
(remaining) units — matching the broker's own "Estimated remaining value".
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.clock import naive_utc_now

log = logging.getLogger(__name__)

_MONEY_RE = re.compile(r"[^\d.\-]")
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def _num(raw: str | None) -> Decimal | None:
    """Parse '10,661' / '$453,461.58' / '61.62' → Decimal, else None."""
    if raw is None:
        return None
    s = _MONEY_RE.sub("", raw.strip())
    if not s or s in ("-", "."):
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _parse_date(raw: str | None) -> date | None:
    if not raw or not _DATE_RE.match(raw.strip()):
        return None
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


@dataclass
class VestTranche:
    vest_date: date
    units: Decimal
    units_unvested: Decimal | None = None
    units_vested: Decimal | None = None


@dataclass
class Grant:
    award_date: date | None
    award_type: str | None
    award_code: str | None
    awarded_units: Decimal | None
    units_unvested: Decimal | None
    units_vested: Decimal | None
    vests: list[VestTranche] = field(default_factory=list)


@dataclass
class ParsedRSU:
    symbol: str | None
    stock_price: Decimal | None
    price_date: date | None
    total_awarded: Decimal | None
    total_unvested: Decimal | None
    total_vested: Decimal | None
    grants: list[Grant] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return bool(self.symbol and self.grants)


def is_merrill_rsu_csv(filepath: str) -> bool:
    """Cheap sniff: the "Your Awards" export has a distinctive header block."""
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as fh:
            head = fh.read(2000).lower()
    except OSError:
        return False
    return (
        "your awards" in head
        and "stock symbol" in head
        and ("restricted stock units" in head or "awarded units" in head)
    )


def parse_merrill_rsu_csv(filepath: str) -> ParsedRSU:
    with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as fh:
        return parse_merrill_rsu_text(fh.read())


def parse_merrill_rsu_text(text: str) -> ParsedRSU:
    rows = list(csv.reader(io.StringIO(text)))

    symbol = stock_price = price_date = None
    total_awarded = total_unvested = total_vested = None
    grants: list[Grant] = []

    def cell(row: list[str], i: int) -> str:
        return row[i].strip() if i < len(row) and row[i] is not None else ""

    for row in rows:
        c0 = cell(row, 0)
        c0l = c0.lower()

        if c0l == "stock symbol":
            symbol = cell(row, 1).upper() or None
            continue
        if c0l == "stock price":
            stock_price = _num(cell(row, 1))
            continue
        if c0l == "stock price as of date":
            price_date = _parse_date(cell(row, 1))
            continue
        if c0l == "total":
            total_awarded = _num(cell(row, 3)) or total_awarded
            total_unvested = _num(cell(row, 4)) or total_unvested
            total_vested = _num(cell(row, 6)) or total_vested
            continue

        # Grant header row: date in col 0, "type / code" in col 1.
        if _DATE_RE.match(c0) and "/" in cell(row, 1):
            type_code = cell(row, 1)
            award_type, _, award_code = type_code.partition("/")
            grants.append(Grant(
                award_date=_parse_date(c0),
                award_type=award_type.strip() or None,
                award_code=award_code.strip() or None,
                awarded_units=_num(cell(row, 3)),
                units_unvested=_num(cell(row, 4)),
                units_vested=_num(cell(row, 6)),
            ))
            continue

        # Vesting sub-row: leading cols blank, a date in col 7.
        c7 = cell(row, 7)
        if grants and not c0 and _DATE_RE.match(c7):
            vd = _parse_date(c7)
            if vd is not None:
                grants[-1].vests.append(VestTranche(
                    vest_date=vd,
                    units=_num(cell(row, 8)) or Decimal("0"),
                    units_unvested=_num(cell(row, 9)),
                    units_vested=_num(cell(row, 11)),
                ))

    # Grand-total summary row ("Restricted Stock Units" block) when Total absent.
    if total_awarded is None:
        for i, row in enumerate(rows):
            if cell(row, 0).lower() == "restricted stock units" and cell(row, 1).lower() == "awarded units":
                nxt = rows[i + 1] if i + 1 < len(rows) else []
                total_awarded = _num(cell(nxt, 1))
                total_unvested = _num(cell(nxt, 2))
                total_vested = _num(cell(nxt, 4))
                break

    return ParsedRSU(
        symbol=symbol,
        stock_price=stock_price,
        price_date=price_date,
        total_awarded=total_awarded,
        total_unvested=total_unvested,
        total_vested=total_vested,
        grants=grants,
    )


def _get_or_create_instrument(db: Session, symbol: str, currency: str):
    from app.models.instrument import Instrument

    inst = db.execute(
        select(Instrument).where(Instrument.symbol == symbol).limit(1)
    ).scalar_one_or_none()
    if inst is None:
        inst = Instrument(
            symbol=symbol, name=symbol, currency=currency, asset_class="equity",
        )
        db.add(inst)
        db.flush()
    return inst


def import_rsu_grants(db: Session, account, parsed: ParsedRSU) -> dict:
    """Persist grants + vesting tranches; refresh the statement price snapshot.

    Idempotent per grant on ``award_code`` — re-importing an updated summary
    replaces that grant's vesting schedule rather than duplicating it. Sets the
    account to value from the latest AssetValuation and writes one immediately.
    """
    from app.models.instrument import PriceSnapshot
    from app.models.rsu import RSUGrant, RSUVest

    if not parsed.is_valid:
        raise ValueError("Not a recognisable RSU award summary (no symbol/grants).")

    currency = account.currency or "USD"
    inst = _get_or_create_instrument(db, parsed.symbol, currency)

    # Record the statement's stock price as a snapshot so valuation has a
    # baseline even before a live fetch.
    if parsed.stock_price:
        snap_date = datetime.combine(
            parsed.price_date or naive_utc_now().date(), datetime.min.time()
        )
        db.add(PriceSnapshot(
            instrument_id=inst.id,
            as_of_date=snap_date,
            price=parsed.stock_price,
            currency=currency,
            source="rsu_statement",
            confidence=0.9,
            stale_flag=False,
        ))

    grants_created = grants_updated = vests_created = 0
    for g in parsed.grants:
        existing = None
        if g.award_code:
            existing = db.execute(
                select(RSUGrant).where(
                    RSUGrant.account_id == account.id,
                    RSUGrant.award_code == g.award_code,
                ).limit(1)
            ).scalar_one_or_none()

        if existing is not None:
            existing.award_date = g.award_date
            existing.award_type = g.award_type
            existing.awarded_units = g.awarded_units
            existing.instrument_id = inst.id
            for v in list(existing.vests):
                db.delete(v)
            db.flush()
            grant = existing
            grants_updated += 1
        else:
            grant = RSUGrant(
                account_id=account.id,
                instrument_id=inst.id,
                award_code=g.award_code,
                award_type=g.award_type,
                award_date=g.award_date,
                awarded_units=g.awarded_units,
                source="merrill_rsu",
            )
            db.add(grant)
            db.flush()
            grants_created += 1

        for v in g.vests:
            db.add(RSUVest(
                grant_id=grant.id,
                vest_date=v.vest_date,
                units=v.units,
                units_unvested=v.units_unvested,
                units_vested=v.units_vested,
            ))
            vests_created += 1

    db.flush()

    # Value from positions going forward.
    from app.models.enums import BalanceTruthSource
    account.balance_truth_source = BalanceTruthSource.LATEST_VALUATION.value
    val = value_rsu_account(db, account, refresh_price=False)
    db.commit()

    return {
        "grants_created": grants_created,
        "grants_updated": grants_updated,
        "vests_created": vests_created,
        "valuation": val,
    }


def _unvested_units(db: Session, account_id: int) -> Decimal:
    from app.models.rsu import RSUGrant, RSUVest

    rows = db.execute(
        select(RSUVest)
        .join(RSUGrant, RSUVest.grant_id == RSUGrant.id)
        .where(RSUGrant.account_id == account_id)
    ).scalars().all()

    today = date.today()
    total = Decimal("0")
    for v in rows:
        if v.units_unvested is not None:
            total += Decimal(v.units_unvested)
        elif v.vest_date > today:
            total += Decimal(v.units)
    return total


def value_rsu_account(
    db: Session, account, refresh_price: bool = True, persist: bool = True,
) -> dict:
    """Value the account at unvested units × current price.

    When ``refresh_price`` is set, fetches a live market price first (and caches
    it as a PriceSnapshot). When ``persist`` is set, writes an AssetValuation
    row (skip it on read-only page views). Returns a summary dict.
    """
    from app.models.asset_valuation import AssetValuation
    from app.models.instrument import Instrument, PriceSnapshot
    from app.models.rsu import RSUGrant

    grant0 = db.execute(
        select(RSUGrant).where(RSUGrant.account_id == account.id).limit(1)
    ).scalar_one_or_none()
    if grant0 is None or grant0.instrument_id is None:
        return {"units_unvested": Decimal("0"), "price": None, "value": Decimal("0")}

    inst = db.get(Instrument, grant0.instrument_id)
    currency = account.currency or (inst.currency if inst else "USD")

    price: Decimal | None = None
    price_source = "snapshot"
    as_of = naive_utc_now()
    if refresh_price and inst is not None:
        try:
            from app.services.price_service import get_current_prices
            prices, asof_map, _live = get_current_prices([inst.symbol], db=db)
            if inst.symbol in prices:
                price = Decimal(str(prices[inst.symbol]))
                price_source = "yfinance_live"
                as_of = asof_map.get(inst.symbol, as_of)
        except Exception as exc:
            log.warning("rsu_service: live price fetch failed for %s: %s", inst.symbol, exc)

    if price is None and inst is not None:
        snap = db.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.instrument_id == inst.id)
            .order_by(PriceSnapshot.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is not None:
            price = Decimal(snap.price)
            as_of = snap.as_of_date

    units = _unvested_units(db, account.id)
    value = (units * price) if price is not None else Decimal("0")
    value = value.quantize(Decimal("0.01"))

    if price is not None and persist:
        db.add(AssetValuation(
            account_id=account.id,
            date=as_of,
            value=value,
            currency=currency,
            source=f"rsu_{price_source}",
            notes=f"{units} unvested units × {price} {currency}",
        ))
        db.flush()

    return {
        "units_unvested": units,
        "price": price,
        "price_source": price_source,
        "currency": currency,
        "value": value,
        "as_of": as_of,
    }
