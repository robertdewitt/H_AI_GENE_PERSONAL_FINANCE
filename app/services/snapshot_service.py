"""Snapshot computation — MTM time series and startup state.

On startup (or on demand), iterates all accounts, computes best-known
balance with truth metadata, and stores snapshots.  Stale values are
stored with stale_flag=True rather than omitted.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from app.services.clock import naive_utc_now
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.account import Account, LIABILITY_TYPES
from app.models.enums import SnapshotSource
from app.models.snapshots import (
    AccountBalanceSnapshot,
    HouseholdSnapshot,
    LiabilityBalanceSnapshot,
)
from app.services.account_service import (
    get_account_balance_rich,
    get_many_account_balances_rich,
)

log = logging.getLogger(__name__)


@dataclass
class StartupStateResult:
    accounts_refreshed: int = 0
    stale_accounts: int = 0
    low_confidence_accounts: int = 0
    household_snapshot_id: int | None = None
    warnings: list[str] = field(default_factory=list)


def compute_household_snapshot(
    db: Session,
    as_of_date: datetime | None = None,
    user_id: int | None = None,
) -> HouseholdSnapshot:
    """Compute and persist a full household balance-sheet snapshot."""
    now = as_of_date or naive_utc_now()
    base_ccy = settings.base_currency

    _q = select(Account)
    if user_id is not None:
        _q = _q.where(Account.user_id == user_id)
    accounts = db.execute(_q).scalars().all()
    total_assets = Decimal("0.00")
    total_liabilities = Decimal("0.00")
    stale_count = 0
    low_conf_count = 0

    # Batch all balances at the snapshot date so this stays bounded in SQL
    # statements rather than O(accounts). When the caller asked for a
    # historical snapshot we route through the series helper for a single
    # date so truth-source dispatch is consistent with time-travelled data.
    if as_of_date is None or as_of_date == now:
        balances = get_many_account_balances_rich(
            db, accounts=accounts, target_currency=base_ccy,
        )
    else:
        from app.services.account_service import get_many_account_balances_series
        series = get_many_account_balances_series(
            db, accounts=accounts, snapshot_dates=[now], target_currency=base_ccy,
        )
        balances = series.get(now, {})

    for acct in accounts:
        result = balances.get(
            acct.id,
            get_account_balance_rich(
                db, acct.id, as_of_date=now, target_currency=base_ccy,
            ),
        )
        source = SnapshotSource.COMPUTED.value
        if result.balance_stale:
            source = SnapshotSource.STALE_CARRYFORWARD.value
            stale_count += 1
        if result.balance_confidence is not None and result.balance_confidence < 0.5:
            low_conf_count += 1

        if acct.account_type in LIABILITY_TYPES or not acct.is_asset:
            snap = LiabilityBalanceSnapshot(
                account_id=acct.id,
                as_of_date=now,
                value_native=result.value,
                value_base=result.value,
                currency=base_ccy,
                source=source,
                confidence=result.balance_confidence,
                stale_flag=result.balance_stale,
            )
            db.add(snap)
            total_liabilities += abs(result.value)
        else:
            snap = AccountBalanceSnapshot(
                account_id=acct.id,
                as_of_date=now,
                value_native=result.value,
                value_base=result.value,
                currency=base_ccy,
                source=source,
                confidence=result.balance_confidence,
                stale_flag=result.balance_stale,
            )
            db.add(snap)
            total_assets += abs(result.value)

    household = HouseholdSnapshot(
        as_of_date=now,
        total_assets_base=total_assets,
        total_liabilities_base=total_liabilities,
        net_worth_base=total_assets - total_liabilities,
        base_currency=base_ccy,
        accounts_included=len(accounts),
        stale_accounts=stale_count,
        low_confidence_accounts=low_conf_count,
        confidence=_aggregate_confidence(len(accounts), stale_count, low_conf_count),
    )
    db.add(household)
    db.flush()
    return household


def compute_startup_state(db: Session) -> StartupStateResult:
    """Run on application startup: refresh all account balances and
    produce a household snapshot. Does NOT fabricate live precision."""
    now = naive_utc_now()
    result = StartupStateResult()

    accounts = db.execute(select(Account)).scalars().all()
    balances = get_many_account_balances_rich(
        db, accounts=accounts, target_currency=settings.base_currency,
    )
    for acct in accounts:
        bal = balances[acct.id]
        result.accounts_refreshed += 1
        if bal.balance_stale:
            result.stale_accounts += 1
            result.warnings.append(
                f"Account '{acct.name}' balance is stale (as_of {bal.balance_as_of})"
            )
        if bal.balance_confidence is not None and bal.balance_confidence < 0.5:
            result.low_confidence_accounts += 1

    household = compute_household_snapshot(db, as_of_date=now)
    db.commit()
    result.household_snapshot_id = household.id

    log.info(
        "Startup state: %d accounts, %d stale, %d low-confidence, snapshot=%d",
        result.accounts_refreshed, result.stale_accounts,
        result.low_confidence_accounts, household.id,
    )
    return result


def _aggregate_confidence(total: int, stale: int, low_conf: int) -> float:
    if total == 0:
        return 0.0
    healthy = total - stale - low_conf
    return max(0.0, min(1.0, healthy / total))
