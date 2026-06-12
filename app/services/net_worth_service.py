"""Net worth computation — FX-aware, works with mixed-currency accounts.

Both ``compute_net_worth`` and ``compute_net_worth_series`` route through
the batch balance helpers in :mod:`app.services.account_service` so the
hot path stays bounded in SQL statements rather than O(months × accounts).
"""
from datetime import datetime, timedelta
from app.services.clock import naive_utc_now
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.account import Account
from app.schemas.net_worth import AccountBalance, NetWorthSnapshot, NetWorthTimeSeries
from app.services.account_service import (
    get_many_account_balances_rich,
    get_many_account_balances_series,
)


def _snapshot_from_balances(
    balances: dict,                       # account_id -> AccountBalanceResult
    accounts: list[Account],
    snapshot_date: datetime,
    base_ccy: str,
) -> NetWorthSnapshot:
    """Fold a per-account balance map into a NetWorthSnapshot.

    Preserves the liability sign convention: stored values are amount owed
    (positive) or, when the account is in credit, the negative of what the
    bank owes the user. We honour that sign so a credit balance improves
    net worth instead of being silently flipped back to debt.
    """
    breakdown: list[AccountBalance] = []
    total_assets = Decimal("0.00")
    total_liabilities = Decimal("0.00")

    for acct in accounts:
        result = balances.get(acct.id)
        balance = result.value if result is not None else Decimal("0.00")

        if acct.is_asset:
            signed_balance = balance
            total_assets += balance  # negative = overdraft, correctly reduces assets
        else:
            signed_balance = -balance  # owed → negative NW; credit → positive NW
            total_liabilities += balance  # net owed (can go negative if in credit)

        breakdown.append(AccountBalance(
            account_id=acct.id,
            account_name=acct.name,
            account_type=acct.account_type.value,
            type_group=acct.type_group,
            balance=signed_balance,
            currency=acct.currency,
            balance_base=balance if acct.currency != base_ccy else None,
            is_asset=acct.is_asset,
            as_of_date=snapshot_date,
        ))

    return NetWorthSnapshot(
        date=snapshot_date,
        currency=base_ccy,
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        net_worth=total_assets - total_liabilities,
        breakdown=breakdown,
    )


def compute_net_worth(
    db: Session,
    as_of_date: datetime | None = None,
    target_currency: str | None = None,
) -> NetWorthSnapshot:
    """Compute net worth snapshot at a given date, converted to target currency.

    When ``as_of_date`` is None the **current** net worth is computed using
    the full account balance (no date filter on transactions).
    """
    snapshot_date = as_of_date or naive_utc_now()
    base_ccy = target_currency or settings.base_currency

    accounts = db.execute(select(Account)).scalars().all()
    if as_of_date is None:
        balances = get_many_account_balances_rich(
            db, accounts=accounts, target_currency=base_ccy,
        )
    else:
        # Historical query: reuse the series helper for a single date so we
        # honour truth-source dispatch against time-travelled data without
        # duplicating the logic.
        series = get_many_account_balances_series(
            db, accounts=accounts, snapshot_dates=[as_of_date],
            target_currency=base_ccy,
        )
        balances = series.get(as_of_date, {})
    return _snapshot_from_balances(balances, accounts, snapshot_date, base_ccy)


def compute_net_worth_series(
    db: Session,
    months: int = 12,
    target_currency: str | None = None,
) -> NetWorthTimeSeries:
    """Compute monthly net worth snapshots for the past N months.

    Uses the batched series helper so the whole timeline is produced with
    a bounded number of SQL statements rather than O(months × accounts).
    """
    now = naive_utc_now()
    base_ccy = target_currency or settings.base_currency

    # Build the list of month-end snapshot dates (capped at "now" for the
    # current month so a partial month isn't projected to a future date).
    start = now - timedelta(days=months * 30)
    snapshot_dates: list[datetime] = []
    current = datetime(start.year, start.month, 1)
    while current <= now:
        last_day = (
            datetime(current.year, current.month + 1, 1) - timedelta(days=1)
            if current.month < 12
            else datetime(current.year, 12, 31)
        )
        snapshot_dates.append(min(last_day, now))
        if current.month == 12:
            current = datetime(current.year + 1, 1, 1)
        else:
            current = datetime(current.year, current.month + 1, 1)

    accounts = db.execute(select(Account)).scalars().all()
    series_balances = get_many_account_balances_series(
        db, accounts=accounts, snapshot_dates=snapshot_dates,
        target_currency=base_ccy,
    )

    snapshots: list[NetWorthSnapshot] = [
        _snapshot_from_balances(
            series_balances.get(d, {}), accounts, d, base_ccy,
        )
        for d in snapshot_dates
    ]
    return NetWorthTimeSeries(snapshots=snapshots)
