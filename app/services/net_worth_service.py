"""Net worth computation — FX-aware, works with mixed-currency accounts."""
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.account import Account
from app.schemas.net_worth import AccountBalance, NetWorthSnapshot, NetWorthTimeSeries
from app.services.account_service import get_account_balance


def compute_net_worth(
    db: Session,
    as_of_date: datetime | None = None,
    target_currency: str | None = None,
) -> NetWorthSnapshot:
    """Compute net worth snapshot at a given date, converted to target currency.

    When ``as_of_date`` is None the **current** net worth is computed using
    the full account balance (no date filter on transactions).
    """
    snapshot_date = as_of_date or datetime.now()
    base_ccy = target_currency or settings.base_currency

    accounts = db.execute(select(Account)).scalars().all()
    breakdown: list[AccountBalance] = []
    total_assets = Decimal("0.00")
    total_liabilities = Decimal("0.00")

    for acct in accounts:
        balance = get_account_balance(
            db, acct.id, as_of_date=as_of_date, target_currency=base_ccy,
        )
        # Liability convention: stored value is amount owed (positive) or, when
        # the account is in credit, the negative of what the bank owes the user.
        # Honour the sign so a credit balance improves net worth instead of
        # silently flipping it back to debt.
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


def compute_net_worth_series(
    db: Session,
    months: int = 12,
    target_currency: str | None = None,
) -> NetWorthTimeSeries:
    """Compute monthly net worth snapshots for the past N months."""
    now = datetime.now()
    base_ccy = target_currency or settings.base_currency
    snapshots: list[NetWorthSnapshot] = []

    start = now - timedelta(days=months * 30)
    current = datetime(start.year, start.month, 1)
    while current <= now:
        last_day = (
            datetime(current.year, current.month + 1, 1) - timedelta(days=1)
            if current.month < 12
            else datetime(current.year, 12, 31)
        )
        snapshot_date = min(last_day, now)
        snapshot = compute_net_worth(db, snapshot_date, target_currency=base_ccy)
        snapshots.append(snapshot)

        if current.month == 12:
            current = datetime(current.year + 1, 1, 1)
        else:
            current = datetime(current.year, current.month + 1, 1)

    return NetWorthTimeSeries(snapshots=snapshots)
