"""Monte Carlo projection for future net worth, broken down by asset group.

Groups projected independently:
  - Investments & Retirement  — log-normal fitted from actual stock-price history
                                (yfinance, current holdings × 5yr monthly closes)
  - Real Estate               — log-normal, regional HPI from FRED
  - Banking (cash)            — log-normal, low default volatility
  - Mortgages                 — deterministic amortisation (principal × rate)
  - Other assets / liabilities

Flags per group when history is unavailable or < 5 years.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from app.services.clock import naive_utc_now
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.account import Account, AccountType, LIABILITY_TYPES
from app.models.asset_valuation import AssetValuation
from app.models.transaction import Transaction
from app.services.account_service import get_account_balance


# ── Default annual parameters (used when history is missing) ─────────────────
_DEFAULTS: dict[str, tuple[float, float]] = {
    "Investments & Retirement": (0.07,  0.15),   # 7 % return, 15 % vol
    "Real Estate":              (0.035, 0.05),   # 3.5 %,       5 %
    "Banking":                  (0.02,  0.005),  # 2 %,       0.5 %
    "Other":                    (0.02,  0.03),   # 2 %,         3 %
}

# Account types that belong to each projection group
_INVESTMENT_TYPES = {
    AccountType.BROKERAGE, AccountType.IRA, AccountType.ROTH_IRA,
    AccountType.PENSION, AccountType.FOUR_OH_ONE_K,
}
_REAL_ESTATE_TYPES = {AccountType.REAL_ESTATE}
_BANKING_TYPES    = {AccountType.CHECKING, AccountType.SAVINGS}
_MORTGAGE_TYPES   = {AccountType.MORTGAGE}
_OTHER_ASSET_TYPES = {AccountType.VEHICLE, AccountType.COLLECTIBLE, AccountType.OTHER}


def _annual_to_monthly(ann_return: float, ann_vol: float) -> tuple[float, float]:
    """Convert annualised log-normal params to monthly."""
    mean = math.log(1 + ann_return) / 12
    std  = ann_vol / math.sqrt(12)
    return mean, std


def _fit_log_returns(values: list[float]) -> tuple[float, float] | None:
    """Fit mean and std of log returns.  Returns None if < 3 usable pairs."""
    log_returns: list[float] = []
    for i in range(1, len(values)):
        prev, curr = values[i - 1], values[i]
        if prev > 0 and curr > 0:
            log_returns.append(math.log(curr / prev))
    if len(log_returns) < 3:
        return None
    mu = sum(log_returns) / len(log_returns)
    var = sum((r - mu) ** 2 for r in log_returns) / len(log_returns)
    return mu, math.sqrt(max(var, 1e-10))


def _percentile(sorted_vals: list[float], p: float) -> float:
    idx = (len(sorted_vals) - 1) * p / 100.0
    lo  = int(idx)
    hi  = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _amortise(
    balance: float,
    monthly_rate: float,
    monthly_payment: float,
    n_months: int,
) -> list[float]:
    """Return deterministic remaining principal at each future month (negative = liability)."""
    result: list[float] = []
    b = abs(balance)
    for _ in range(n_months):
        if b <= 0:
            result.append(0.0)
            continue
        interest   = b * monthly_rate
        principal  = max(0.0, monthly_payment - interest)
        b          = max(0.0, b - principal)
        result.append(-b)          # kept negative — it is a liability
    return result


# ── Investment history via stock prices ───────────────────────────────────────

def _get_investment_monthly_values(
    db: "Session",
    acct_ids: list[int],
    history_months: int = 60,
) -> tuple[list[float], str, bool, str | None]:
    """Build monthly portfolio values for investment accounts using Yahoo Finance.

    Always downloads ``history_months`` of daily closes for every symbol
    in the accounts' PositionLots, then multiplies by current quantities.
    This gives the full 5-year history regardless of how far back trades
    were imported.

    Falls back to AssetValuation rows or defaults if no positions exist.

    Returns:
        monthly_values  — end-of-month portfolio values, oldest first
        source          — human-readable data source description
        flagged         — True if history < 60 months
        flag_reason     — shown to user when flagged
    """
    import logging
    from datetime import timedelta

    import yfinance as yf
    from sqlalchemy import select

    from app.models.instrument import Instrument, PositionLot

    log = logging.getLogger(__name__)
    now = naive_utc_now()
    lookback_start = now - timedelta(days=history_months * 31)

    # ── All positions across these accounts ──────────────────────────────────
    pos_rows = db.execute(
        select(PositionLot, Instrument.symbol)
        .join(Instrument, PositionLot.instrument_id == Instrument.id)
        .where(PositionLot.account_id.in_(acct_ids), PositionLot.quantity != 0)
    ).all()

    qty_by_symbol: dict[str, float] = {}
    for lot, symbol in pos_rows:
        qty_by_symbol[symbol] = qty_by_symbol.get(symbol, 0.0) + float(lot.quantity)

    if not qty_by_symbol:
        return [], "no position data", True, "No stock positions found — using default parameters"

    symbols = sorted(qty_by_symbol)

    # ── Download full history from Yahoo Finance ──────────────────────────────
    combined_daily: dict[str, float] = {}
    try:
        raw = yf.download(
            symbols,
            start=lookback_start.strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=False,
        )
        if raw is None or raw.empty:
            raise ValueError("empty response from Yahoo Finance")

        if hasattr(raw.columns, "levels"):
            close_df = raw["Close"].ffill()
        else:
            close_df = raw[["Close"]].ffill()
            close_df.columns = symbols

        for ts in close_df.index:
            date_str = ts.strftime("%Y-%m-%d")
            total = 0.0
            for sym in symbols:
                q = qty_by_symbol.get(sym, 0.0)
                if q == 0.0:
                    continue
                try:
                    p = float(close_df.loc[ts, sym])
                    if p > 0:
                        total += q * p
                except (KeyError, TypeError):
                    pass
            if total > 0:
                combined_daily[date_str] = total

    except Exception as exc:
        log.warning("monte_carlo: Yahoo Finance download failed for %s: %s", symbols, exc)
        return (
            [], f"Yahoo Finance unavailable ({exc})", True,
            f"Could not fetch price history from Yahoo Finance — using default parameters"
        )

    if not combined_daily:
        return [], "no position data", True, "No stock positions found — using default parameters"

    # ── Downsample to monthly (last available day each month) ─────────────────
    monthly: dict[str, float] = {}
    for d in sorted(combined_daily):
        ym = d[:7]   # "YYYY-MM"
        monthly[ym] = combined_daily[d]   # last date in month wins

    monthly_values = [monthly[ym] for ym in sorted(monthly)]
    months = len(monthly_values)

    # Build source description
    symbols_held: list[str] = []
    try:
        all_pos = db.execute(
            select(Instrument.symbol.distinct())
            .join(PositionLot, PositionLot.instrument_id == Instrument.id)
            .where(PositionLot.account_id.in_(acct_ids), PositionLot.quantity != 0)
        ).scalars().all()
        symbols_held = sorted(all_pos)
    except Exception:
        pass

    sym_str = ", ".join(symbols_held[:6]) + ("…" if len(symbols_held) > 6 else "")
    source  = f"yfinance historical prices ({sym_str}; {months} months)"

    flagged     = months < 60
    flag_reason = (
        f"Only {months} months of price history available (5 years recommended)"
        if flagged else None
    )
    return monthly_values, source, flagged, flag_reason


def _simulate_investment_group(
    acct_list: list,
    current_balances: dict[int, float],
    val_history: dict,
    db: "Session",
    now: datetime,
    horizon_months: int,
    simulations: int,
) -> "GroupProjection":
    """Project investment accounts using actual historical stock prices."""
    ann_return_default, ann_vol_default = _DEFAULTS["Investments & Retirement"]

    current_total = sum(current_balances.get(a.id, 0.0) for a in acct_list)
    acct_ids      = [a.id for a in acct_list]

    # Try to get monthly values from stock prices
    monthly_values, source, flagged, flag_reason = _get_investment_monthly_values(
        db, acct_ids
    )

    params = _fit_log_returns(monthly_values) if len(monthly_values) >= 4 else None

    if params is None:
        # Fall back to AssetValuation history
        all_vals: list[float] = []
        earliest: datetime | None = None
        for acct in acct_list:
            hist = val_history.get(acct.id, [])
            if len(hist) >= 2:
                base = hist[0][1]
                if base > 0:
                    all_vals.extend(v / base * 1000 for _, v in hist)
                    if earliest is None or hist[0][0] < earliest:
                        earliest = hist[0][0]
        params = _fit_log_returns(all_vals) if len(all_vals) >= 4 else None
        if params is not None:
            months_hist = max(1, int((now - earliest).days / 30.44)) if earliest else 0  # type: ignore[arg-type]
            source  = f"asset valuations ({months_hist} months)"
            flagged = months_hist < 60
            flag_reason = (
                f"Only {months_hist} months of valuation history (5 years recommended)"
                if flagged else None
            )
        else:
            mu_m, std_m = _annual_to_monthly(ann_return_default, ann_vol_default)
            flagged = True
            flag_reason = (
                f"No stock or valuation history — "
                f"using defaults ({ann_return_default*100:.0f}%/yr, "
                f"{ann_vol_default*100:.0f}% vol)"
            )
            source = "defaults"

    if params is not None:
        mu_m, std_m = params
    else:
        mu_m, std_m = _annual_to_monthly(ann_return_default, ann_vol_default)

    ann_return_pct = round((math.exp(mu_m * 12) - 1) * 100, 2)
    ann_vol_pct    = round(std_m * math.sqrt(12) * 100, 2)

    if current_total == 0:
        return GroupProjection(
            name="Investments & Retirement", key="investments",
            current_value=0.0, median=[0.0] * horizon_months,
            flagged=True, flag_reason="Zero balance",
            ann_return_pct=ann_return_pct, ann_volatility_pct=ann_vol_pct,
            deterministic=False, months_of_history=len(monthly_values),
            source_note=source,
        )

    all_paths: list[list[float]] = []
    for _ in range(simulations):
        path: list[float] = []
        v = current_total
        for _ in range(horizon_months):
            v = v * math.exp(mu_m + std_m * random.gauss(0, 1))
            path.append(v)
        all_paths.append(path)

    median = [
        round(_percentile(sorted(p[s] for p in all_paths), 50), 2)
        for s in range(horizon_months)
    ]

    return GroupProjection(
        name="Investments & Retirement",
        key="investments",
        current_value=round(current_total, 2),
        median=median,
        flagged=flagged,
        flag_reason=flag_reason,
        ann_return_pct=ann_return_pct,
        ann_volatility_pct=ann_vol_pct,
        deterministic=False,
        months_of_history=len(monthly_values),
        source_note=source,
    )


# ── Public dataclasses ────────────────────────────────────────────────────────

@dataclass
class GroupProjection:
    name: str                   # short display label for chart legend
    key: str                    # slug for JS
    current_value: float        # value today (negative for liabilities)
    median: list[float]         # P50 at each future step
    flagged: bool
    flag_reason: str | None
    ann_return_pct: float | None
    ann_volatility_pct: float | None
    deterministic: bool         # True for mortgage amortisation
    months_of_history: int
    source_note: str = ""       # human-readable data source (shown in stats row)


@dataclass
class MonteCarloResult:
    current_nw: float
    flagged: bool
    flag_reason: str | None
    horizon_months: int
    simulations: int
    dates: list[str]
    total_p10: list[float]
    total_p50: list[float]
    total_p90: list[float]
    groups: list[GroupProjection]


# ── Real-estate group helper (uses live HPI data) ────────────────────────────

def _simulate_re_group(
    acct_list: list,
    current_balances: dict[int, float],
    val_history: dict,
    get_hpi_for_address,   # callable: address -> HPIResult
    now: datetime,
    horizon_months: int,
    simulations: int,
) -> "GroupProjection":
    """Project real estate using per-property HPI data from the internet.

    Each property gets its own HPI lookup.  Returns a single grouped
    GroupProjection whose median is the sum of per-property medians and
    whose flag reflects any failed lookups.
    """
    import math, random as _random

    RE_VOL = 0.05   # 5 % annual vol (same regardless of HPI source)

    property_medians: list[list[float]] = []
    flags: list[str] = []
    sources: list[str] = []

    for acct in acct_list:
        current_val = current_balances.get(acct.id, 0.0)
        if current_val == 0.0:
            property_medians.append([0.0] * horizon_months)
            continue

        hpi = get_hpi_for_address(acct.property_address)
        if hpi.flagged:
            flags.append(
                f"{acct.name}: {hpi.flag_reason}"
            )
        sources.append(hpi.source)

        # Check if we have enough valuation history to override HPI
        hist = val_history.get(acct.id, [])
        if len(hist) >= 10:
            # Prefer fitted returns from actual valuations
            vals = [float(v) for _, v in hist if float(v) > 0]
            params = _fit_log_returns(vals)
        else:
            params = None

        if params is not None:
            mu_m, std_m = params
        else:
            mu_m, std_m = _annual_to_monthly(hpi.annual_return, RE_VOL)

        # Run simulations for this property
        all_paths: list[list[float]] = []
        for _ in range(simulations):
            path: list[float] = []
            v = current_val
            for _ in range(horizon_months):
                v = v * math.exp(mu_m + std_m * _random.gauss(0, 1))
                path.append(v)
            all_paths.append(path)

        prop_median = [
            round(_percentile(sorted(p[s] for p in all_paths), 50), 2)
            for s in range(horizon_months)
        ]
        property_medians.append(prop_median)

    # Sum medians across all properties
    if not property_medians:
        combined_median = [0.0] * horizon_months
    else:
        combined_median = [
            round(sum(pm[i] for pm in property_medians), 2)
            for i in range(horizon_months)
        ]

    current_total = sum(current_balances.get(a.id, 0.0) for a in acct_list)
    flagged       = bool(flags)

    if flags:
        flag_reason = "; ".join(flags)
    elif sources:
        flag_reason = None
    else:
        flag_reason = None

    # Compute blended annualised return for display
    if property_medians and current_total > 0 and horizon_months > 0:
        final_median = combined_median[-1]
        ann_return   = round(((final_median / current_total) ** (12 / horizon_months) - 1) * 100, 2)
    else:
        ann_return = round((_DEFAULTS["Real Estate"][0]) * 100, 2)

    source_note = " | ".join(dict.fromkeys(sources))   # deduplicated
    return GroupProjection(
        name="Real Estate",
        key="real_estate",
        current_value=round(current_total, 2),
        median=combined_median,
        flagged=flagged,
        flag_reason=flag_reason,
        ann_return_pct=ann_return,
        ann_volatility_pct=round(RE_VOL * 100, 1),
        deterministic=False,
        months_of_history=0,
        source_note=source_note,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def run_monte_carlo(
    db: Session,
    horizon_months: int = 60,
    simulations: int    = 1000,
    user_id: int | None = None,
) -> MonteCarloResult:
    """Run Monte Carlo net worth projection broken down by asset group."""

    now = naive_utc_now()
    _q = select(Account)
    if user_id is not None:
        _q = _q.where(Account.user_id == user_id)
    accounts: list[Account] = db.execute(_q).scalars().all()

    # ── Current balances per account ─────────────────────────────────────────
    current_balances: dict[int, float] = {}
    for acct in accounts:
        bal = get_account_balance(db, acct.id)
        signed = float(bal) if acct.is_asset else -abs(float(bal))
        current_balances[acct.id] = signed

    current_nw = sum(current_balances.values())

    # ── Fetch all AssetValuation rows once ───────────────────────────────────
    val_rows = db.execute(
        select(AssetValuation).order_by(
            AssetValuation.account_id, AssetValuation.date
        )
    ).scalars().all()

    # Group valuation history by account_id: list of (date, value_native)
    val_history: dict[int, list[tuple[datetime, float]]] = {}
    for row in val_rows:
        val_history.setdefault(row.account_id, []).append(
            (row.date, float(row.value))
        )

    # ── Build per-group simulation parameters ────────────────────────────────
    groups: list[GroupProjection] = []

    # Helper: simulate a group of accounts together
    def _simulate_group(
        group_name: str,
        group_key: str,
        acct_list: list[Account],
        default_ann_return: float,
        default_ann_vol: float,
    ) -> GroupProjection:
        current_total = sum(current_balances[a.id] for a in acct_list)

        # Gather valuation histories for accounts in this group that have them
        all_vals: list[float] = []
        earliest_valuation: datetime | None = None
        for acct in acct_list:
            hist = val_history.get(acct.id, [])
            if len(hist) >= 2:
                # Normalise: express as fraction of first value so we can pool returns
                base = hist[0][1]
                if base > 0:
                    all_vals.extend(v / base * 1000 for _, v in hist)
                    if earliest_valuation is None or hist[0][0] < earliest_valuation:
                        earliest_valuation = hist[0][0]

        months_history = (
            max(1, int((now - earliest_valuation).days / 30.44))
            if earliest_valuation else 0
        )

        params = _fit_log_returns(all_vals) if len(all_vals) >= 4 else None

        flagged    = earliest_valuation is None or months_history < 60
        flag_reason: str | None = None
        if params is None:
            mu_m, std_m = _annual_to_monthly(default_ann_return, default_ann_vol)
            flagged = True
            flag_reason = (
                f"No valuation history — using defaults "
                f"({default_ann_return*100:.0f}%/yr return, "
                f"{default_ann_vol*100:.0f}%/yr volatility)"
            )
        else:
            mu_m, std_m = params
            if flagged:
                flag_reason = (
                    f"Only {months_history} months of valuation history "
                    "(5+ years recommended)"
                )

        ann_return_pct    = round((math.exp(mu_m * 12) - 1) * 100, 2)
        ann_vol_pct       = round(std_m * math.sqrt(12) * 100, 2)

        if current_total == 0:
            return GroupProjection(
                name=group_name, key=group_key,
                current_value=0.0,
                median=[0.0] * horizon_months,
                flagged=True, flag_reason="Zero balance — nothing to project",
                ann_return_pct=ann_return_pct,
                ann_volatility_pct=ann_vol_pct,
                deterministic=False,
                months_of_history=months_history,
            )

        # Run simulations
        all_paths: list[list[float]] = []
        for _ in range(simulations):
            path: list[float] = []
            v = current_total
            for _ in range(horizon_months):
                v = v * math.exp(mu_m + std_m * random.gauss(0, 1))
                path.append(v)
            all_paths.append(path)

        median = [
            round(_percentile(sorted(p[s] for p in all_paths), 50), 2)
            for s in range(horizon_months)
        ]

        return GroupProjection(
            name=group_name, key=group_key,
            current_value=round(current_total, 2),
            median=median,
            flagged=flagged, flag_reason=flag_reason,
            ann_return_pct=ann_return_pct,
            ann_volatility_pct=ann_vol_pct,
            deterministic=False,
            months_of_history=months_history,
        )

    # ── Investments & Retirement — fitted from actual stock prices ───────────
    inv_accounts = [a for a in accounts if a.account_type in _INVESTMENT_TYPES]
    if inv_accounts:
        groups.append(_simulate_investment_group(
            inv_accounts, current_balances, val_history,
            db, now, horizon_months, simulations,
        ))

    # ── Real Estate — per-property HPI from internet ─────────────────────────
    re_accounts = [a for a in accounts if a.account_type in _REAL_ESTATE_TYPES]
    if re_accounts:
        from app.services.real_estate_hpi import get_hpi_for_address
        groups.append(_simulate_re_group(
            re_accounts, current_balances, val_history,
            get_hpi_for_address, now, horizon_months, simulations,
        ))

    # ── Banking ───────────────────────────────────────────────────────────────
    bank_accounts = [a for a in accounts if a.account_type in _BANKING_TYPES]
    if bank_accounts:
        groups.append(_simulate_group(
            "Banking & Cash", "banking",
            bank_accounts, *_DEFAULTS["Banking"],
        ))

    # ── Mortgages — deterministic amortisation ────────────────────────────────
    mortgage_accounts = [a for a in accounts if a.account_type in _MORTGAGE_TYPES]
    if mortgage_accounts:
        # Aggregate across all mortgages
        total_bal   = sum(current_balances[a.id] for a in mortgage_accounts)
        total_pmt   = sum(
            float(a.monthly_payment) if a.monthly_payment else 0.0
            for a in mortgage_accounts
        )
        # Weighted average monthly rate
        rates_and_bals = [
            (a.interest_rate / 12, abs(current_balances[a.id]))
            for a in mortgage_accounts
            if a.interest_rate and a.interest_rate > 0 and current_balances[a.id] != 0
        ]
        if rates_and_bals:
            total_weighted_bal = sum(b for _, b in rates_and_bals)
            avg_monthly_rate   = (
                sum(r * b for r, b in rates_and_bals) / total_weighted_bal
                if total_weighted_bal > 0 else 0.004
            )
        else:
            avg_monthly_rate = 0.004   # fallback ~4.8% annual

        if total_pmt == 0:
            # Estimate payment from recent transactions
            total_pmt_est = 0.0
            for acct in mortgage_accounts:
                recent_pmts = db.execute(
                    select(func.avg(Transaction.amount))
                    .where(
                        Transaction.account_id == acct.id,
                        Transaction.amount > 0,
                    )
                ).scalar() or 0.0
                total_pmt_est += float(recent_pmts)
            total_pmt = total_pmt_est if total_pmt_est > 0 else abs(total_bal) * avg_monthly_rate * 1.2

        flagged     = False
        flag_reason = None
        missing_rate = any(
            not a.interest_rate or a.interest_rate <= 0
            for a in mortgage_accounts
        )
        if missing_rate:
            flagged     = True
            flag_reason = "Interest rate not set on one or more mortgages — using estimated rate"

        median = _amortise(total_bal, avg_monthly_rate, total_pmt, horizon_months)

        groups.append(GroupProjection(
            name="Mortgages", key="mortgage",
            current_value=round(total_bal, 2),
            median=median,
            flagged=flagged, flag_reason=flag_reason,
            ann_return_pct=None,
            ann_volatility_pct=None,
            deterministic=True,
            months_of_history=0,
        ))

    # ── Other assets (vehicles, collectibles, other) ──────────────────────────
    other_accounts = [
        a for a in accounts
        if a.account_type in _OTHER_ASSET_TYPES
        and a.is_asset
        and current_balances.get(a.id, 0) != 0
    ]
    if other_accounts:
        groups.append(_simulate_group(
            "Other Assets", "other",
            other_accounts, *_DEFAULTS["Other"],
        ))

    # ── Other liabilities (credit cards, loans — not mortgages) ───────────────
    other_liab = [
        a for a in accounts
        if a.account_type in LIABILITY_TYPES
        and a.account_type not in _MORTGAGE_TYPES
        and current_balances.get(a.id, 0) != 0
    ]
    if other_liab:
        # Treat as constant (no projection — user pays them off)
        total_liab = sum(current_balances[a.id] for a in other_liab)
        groups.append(GroupProjection(
            name="Credit & Loans", key="credit_loans",
            current_value=round(total_liab, 2),
            median=[round(total_liab, 2)] * horizon_months,
            flagged=False, flag_reason=None,
            ann_return_pct=None,
            ann_volatility_pct=None,
            deterministic=True,
            months_of_history=0,
        ))

    # ── Total portfolio uncertainty bands ─────────────────────────────────────
    # Re-run combined simulation to get P10/P50/P90 for total NW
    # Identify stochastic groups
    stochastic: list[tuple[float, float, float]] = []   # (current, mu_m, std_m)
    deterministic_series: list[list[float]] = []

    for g in groups:
        if g.deterministic or g.current_value == 0:
            deterministic_series.append(g.median)
        else:
            # Recover monthly params from annualised figures for re-simulation
            if g.ann_return_pct is not None and g.ann_volatility_pct is not None:
                mu_m  = math.log(1 + g.ann_return_pct / 100) / 12
                std_m = (g.ann_volatility_pct / 100) / math.sqrt(12)
                stochastic.append((g.current_value, mu_m, std_m))

    det_sums = [
        sum(s[i] for s in deterministic_series)
        for i in range(horizon_months)
    ] if deterministic_series else [0.0] * horizon_months

    # Run combined simulations
    combined_paths: list[list[float]] = []
    for _ in range(simulations):
        path: list[float] = []
        # Each stochastic group gets its own independent random walk
        group_vals = [cv for cv, _, _ in stochastic]
        for step in range(horizon_months):
            for j, (_, mu_m, std_m) in enumerate(stochastic):
                group_vals[j] = group_vals[j] * math.exp(
                    mu_m + std_m * random.gauss(0, 1)
                )
            path.append(sum(group_vals) + det_sums[step])
        combined_paths.append(path)

    # Fallback if no stochastic groups at all
    if not combined_paths:
        total_p50 = det_sums[:]
        total_p10 = det_sums[:]
        total_p90 = det_sums[:]
    else:
        total_p10 = [
            round(_percentile(sorted(p[s] for p in combined_paths), 10), 2)
            for s in range(horizon_months)
        ]
        total_p50 = [
            round(_percentile(sorted(p[s] for p in combined_paths), 50), 2)
            for s in range(horizon_months)
        ]
        total_p90 = [
            round(_percentile(sorted(p[s] for p in combined_paths), 90), 2)
            for s in range(horizon_months)
        ]

    # ── Date labels ───────────────────────────────────────────────────────────
    dates: list[str] = []
    d = now
    for _ in range(horizon_months):
        d = datetime(d.year + (1 if d.month == 12 else 0),
                     (d.month % 12) + 1, 1)
        dates.append(d.strftime("%b %Y"))

    # ── Overall flag ─────────────────────────────────────────────────────────
    any_flagged = any(g.flagged for g in groups)
    overall_flag_reason = (
        "Some asset groups have limited history — see per-group flags below"
        if any_flagged else None
    )

    return MonteCarloResult(
        current_nw=round(current_nw, 2),
        flagged=any_flagged,
        flag_reason=overall_flag_reason,
        horizon_months=horizon_months,
        simulations=simulations,
        dates=dates,
        total_p10=total_p10,
        total_p50=total_p50,
        total_p90=total_p90,
        groups=groups,
    )
