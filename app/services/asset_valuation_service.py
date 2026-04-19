"""Asset valuation management — manual entry + future API hooks."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account, AccountType
from app.models.asset_valuation import AssetValuation
from app.services.fx_service import convert_amount

NON_TRANSACTIONAL_TYPES = {
    AccountType.REAL_ESTATE,
    AccountType.VEHICLE,
    AccountType.COLLECTIBLE,
    AccountType.PENSION,
}

VALUATION_SOURCES = [
    "manual",
    "zillow",       # placeholder
    "kbb",          # placeholder
    "redfin",       # placeholder
    "appraisal",
    "market_data",  # placeholder for brokerage mark-to-market
]


def add_valuation(
    db: Session,
    account_id: int,
    date: datetime,
    value: float,
    currency: str = "USD",
    source: str = "manual",
    notes: str | None = None,
) -> AssetValuation:
    """Record a point-in-time valuation for an asset."""
    val = AssetValuation(
        account_id=account_id,
        date=date,
        value=value,
        currency=currency,
        source=source,
        notes=notes,
    )
    db.add(val)

    account = db.get(Account, account_id)
    if account:
        account.current_value = value
        account.value_as_of_date = date

    db.commit()
    db.refresh(val)
    return val


def get_valuation_history(
    db: Session,
    account_id: int,
    limit: int = 100,
) -> list[AssetValuation]:
    return db.execute(
        select(AssetValuation)
        .where(AssetValuation.account_id == account_id)
        .order_by(AssetValuation.date.desc())
        .limit(limit)
    ).scalars().all()


def get_latest_valuation(
    db: Session,
    account_id: int,
    as_of_date: datetime | None = None,
) -> AssetValuation | None:
    query = select(AssetValuation).where(
        AssetValuation.account_id == account_id
    )
    if as_of_date:
        query = query.where(AssetValuation.date <= as_of_date)
    query = query.order_by(AssetValuation.date.desc()).limit(1)
    return db.execute(query).scalar_one_or_none()


def get_valuation_in_base_currency(
    db: Session,
    account_id: int,
    base_currency: str = "USD",
    as_of_date: datetime | None = None,
) -> float:
    """Return the latest valuation converted to the base currency."""
    val = get_latest_valuation(db, account_id, as_of_date)
    if not val:
        account = db.get(Account, account_id)
        return account.current_value or 0.0 if account else 0.0

    if val.currency == base_currency:
        return val.value

    converted, _ = convert_amount(
        db, val.value, val.currency, base_currency, val.date
    )
    return converted if converted is not None else val.value


def update_valuation(
    db: Session,
    valuation_id: int,
    date: datetime,
    value: float,
    currency: str,
    source: str,
    notes: str | None,
) -> AssetValuation | None:
    val = db.get(AssetValuation, valuation_id)
    if not val:
        return None
    val.date = date
    val.value = value
    val.currency = currency
    val.source = source
    val.notes = notes

    # Keep account.current_value in sync if this is the latest entry
    account = db.get(Account, val.account_id)
    if account:
        from sqlalchemy import select as _sel
        latest = db.execute(
            _sel(AssetValuation)
            .where(AssetValuation.account_id == val.account_id)
            .order_by(AssetValuation.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest and latest.id == val.id:
            account.current_value = value
            account.value_as_of_date = date

    db.commit()
    db.refresh(val)
    return val


def delete_valuation(db: Session, valuation_id: int) -> bool:
    val = db.get(AssetValuation, valuation_id)
    if not val:
        return False
    account_id = val.account_id
    db.delete(val)
    db.flush()

    # Re-sync current_value to the next most recent valuation
    account = db.get(Account, account_id)
    if account:
        from sqlalchemy import select as _sel
        latest = db.execute(
            _sel(AssetValuation)
            .where(AssetValuation.account_id == account_id)
            .order_by(AssetValuation.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest:
            account.current_value = latest.value
            account.value_as_of_date = latest.date
        else:
            account.current_value = None
            account.value_as_of_date = None

    db.commit()
    return True


def list_valuatable_accounts(db: Session) -> list[Account]:
    """Return accounts that support manual valuation."""
    return db.execute(
        select(Account)
        .where(Account.account_type.in_(NON_TRANSACTIONAL_TYPES))
        .order_by(Account.name)
    ).scalars().all()
