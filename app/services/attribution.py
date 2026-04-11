"""Attribution engine — explain net worth change using flows + valuations + FX."""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.account import Account, ASSET_TYPES
from app.models.asset_valuation import AssetValuation
from app.models.enums import EconomicEventType
from app.models.snapshots import HouseholdSnapshot
from app.models.transaction import Transaction
from app.services.fx_service import convert_amount


@dataclass
class AttributionComponent:
    label: str
    amount_base: float = 0.0
    confidence: float | None = None
    notes: str | None = None


@dataclass
class NetWorthAttribution:
    period_start: datetime | None = None
    period_end: datetime | None = None
    nw_start: float = 0.0
    nw_end: float = 0.0
    delta_nw: float = 0.0
    components: list[AttributionComponent] = field(default_factory=list)
    unexplained: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _latest_valuation_on_or_before(
    db: Session, account_id: int, as_of: datetime,
) -> AssetValuation | None:
    return db.execute(
        select(AssetValuation)
        .where(
            AssetValuation.account_id == account_id,
            AssetValuation.date <= as_of,
        )
        .order_by(AssetValuation.date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _valuation_in_base(
    db: Session, v: AssetValuation, as_of: datetime,
) -> tuple[float | None, float | None]:
    base = settings.base_currency
    if v.currency == base:
        return v.value, 1.0
    conv, rate = convert_amount(db, v.value, v.currency, base, as_of)
    return conv, rate


def compute_market_movement_from_valuations(
    db: Session, start_date: datetime, end_date: datetime,
) -> tuple[float, float]:
    """Sum of (end valuation - start valuation) in base, per account with data.

    Returns (movement, confidence 0..1).
    """
    total = 0.0
    used = 0
    skipped = 0
    accounts = db.execute(select(Account)).scalars().all()
    for acct in accounts:
        if acct.account_type not in ASSET_TYPES:
            continue
        v0 = _latest_valuation_on_or_before(db, acct.id, start_date)
        v1 = _latest_valuation_on_or_before(db, acct.id, end_date)
        if not v0 or not v1:
            skipped += 1
            continue
        b0, _ = _valuation_in_base(db, v0, start_date)
        b1, _ = _valuation_in_base(db, v1, end_date)
        if b0 is None or b1 is None:
            skipped += 1
            continue
        total += b1 - b0
        used += 1
    conf = 0.75 if used > 0 else 0.0
    if skipped > used and used > 0:
        conf *= 0.8
    return total, conf


def _balance_native_as_of(
    db: Session, account_id: int, as_of: datetime,
) -> float:
    raw = db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
            Transaction.account_id == account_id,
            Transaction.date <= as_of,
        )
    ).scalar()
    return float(raw or 0.0)


def compute_fx_translation_effect(
    db: Session, start_date: datetime, end_date: datetime,
) -> tuple[float, float]:
    """Approximate FX P&L: same native balance valued at start vs end rates.

    Uses cumulative transaction balance at end_date as the native notional.
    Returns (fx_movement, confidence).
    """
    base = settings.base_currency
    total = 0.0
    n = 0
    accounts = db.execute(select(Account)).scalars().all()
    for acct in accounts:
        ccy = acct.currency
        if ccy == base:
            continue
        bal = _balance_native_as_of(db, acct.id, end_date)
        if bal == 0.0:
            continue
        c0, r0 = convert_amount(db, bal, ccy, base, start_date)
        c1, r1 = convert_amount(db, bal, ccy, base, end_date)
        if c0 is None or c1 is None:
            continue
        total += c1 - c0
        n += 1
    conf = min(0.85, 0.45 + 0.05 * n) if n > 0 else 0.0
    return total, conf


def attribute_nw_change(
    db: Session,
    start_date: datetime,
    end_date: datetime,
) -> NetWorthAttribution:
    """Decompose net-worth change between two household snapshots."""
    start_snap = db.execute(
        select(HouseholdSnapshot)
        .where(HouseholdSnapshot.as_of_date <= start_date)
        .order_by(HouseholdSnapshot.as_of_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    end_snap = db.execute(
        select(HouseholdSnapshot)
        .where(HouseholdSnapshot.as_of_date <= end_date)
        .order_by(HouseholdSnapshot.as_of_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    result = NetWorthAttribution(period_start=start_date, period_end=end_date)

    if not start_snap or not end_snap:
        result.warnings.append("Missing household snapshots for attribution period")
        return result

    result.nw_start = start_snap.net_worth_base
    result.nw_end = end_snap.net_worth_base
    result.delta_nw = result.nw_end - result.nw_start

    date_filter = and_(Transaction.date >= start_date, Transaction.date <= end_date)

    income_types = [
        EconomicEventType.EXTERNAL_INCOME.value,
        EconomicEventType.PAYROLL_INCOME.value,
        EconomicEventType.EMPLOYER_BENEFIT.value,
    ]
    contributions = _sum_by_event_types(db, income_types, date_filter)
    result.components.append(
        AttributionComponent("contributions", contributions, confidence=0.8)
    )

    inv_types = [
        EconomicEventType.INVESTMENT_CONTRIBUTION.value,
        EconomicEventType.INVESTMENT_WITHDRAWAL.value,
        EconomicEventType.INVESTMENT_FLOW.value,
    ]
    inv_flow = _sum_by_event_types(db, inv_types, date_filter)
    result.components.append(
        AttributionComponent("investment_flows", inv_flow, confidence=0.65)
    )

    fee_types = [
        EconomicEventType.FEE.value,
        EconomicEventType.MORTGAGE_INTEREST.value,
    ]
    fees = _sum_by_event_types(db, fee_types, date_filter)
    result.components.append(
        AttributionComponent("fees_and_interest", fees, confidence=0.7)
    )

    principal = _sum_by_event_types(
        db, [EconomicEventType.MORTGAGE_PRINCIPAL.value], date_filter,
    )
    result.components.append(
        AttributionComponent("principal_paydown", principal, confidence=0.6)
    )

    tax_total = _sum_by_event_types(
        db, [EconomicEventType.TAX_PAYMENT.value], date_filter,
    )
    result.components.append(
        AttributionComponent("tax_payments", tax_total, confidence=0.8)
    )

    spending = _sum_by_event_types(
        db, [EconomicEventType.LIFESTYLE_EXPENSE.value], date_filter,
    )
    result.components.append(
        AttributionComponent("spending", spending, confidence=0.7)
    )

    mkt, mkt_conf = compute_market_movement_from_valuations(
        db, start_date, end_date,
    )
    result.components.append(
        AttributionComponent(
            "market_movement",
            mkt,
            confidence=mkt_conf,
            notes="From AssetValuation diffs (accounts with start+end marks)",
        )
    )

    fx, fx_conf = compute_fx_translation_effect(db, start_date, end_date)
    result.components.append(
        AttributionComponent(
            "fx_movement",
            fx,
            confidence=fx_conf,
            notes="Translation of period-end native balances at start vs end FX",
        )
    )

    explained = sum(c.amount_base for c in result.components)
    result.unexplained = result.delta_nw - explained
    if abs(result.unexplained) > 1.0:
        result.warnings.append(
            f"Residual after attribution: {result.unexplained:.2f} "
            f"(flows, timing, or missing marks)"
        )

    return result


def _sum_by_event_types(db: Session, event_types: list[str], date_filter) -> float:
    raw = db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0.0))
        .where(date_filter, Transaction.event_type.in_(event_types))
    ).scalar()
    return float(raw or 0.0)
