"""Account CRUD and balance computation — FX aware."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.account import Account, AccountType, LIABILITY_TYPES
from app.models.asset_valuation import AssetValuation
from app.models.transaction import Transaction
from app.schemas.account import AccountCreate, AccountUpdate
from app.services.fx_service import convert_amount


TRANSACTIONAL_TYPES = {
    AccountType.CHECKING,
    AccountType.SAVINGS,
    AccountType.CREDIT_CARD,
    AccountType.BROKERAGE,
    AccountType.IRA,
    AccountType.ROTH_IRA,
    AccountType.FOUR_OH_ONE_K,
    AccountType.LOAN,
    AccountType.MORTGAGE,
}


def list_accounts(db: Session) -> list[Account]:
    return db.execute(
        select(Account).order_by(Account.account_type, Account.name)
    ).scalars().all()


def get_account(db: Session, account_id: int) -> Account | None:
    return db.get(Account, account_id)


def create_account(db: Session, data: AccountCreate) -> Account:
    is_asset = data.account_type not in LIABILITY_TYPES
    if data.is_asset is not None:
        is_asset = data.is_asset

    account = Account(
        name=data.name,
        account_type=data.account_type,
        institution=data.institution,
        currency=data.currency,
        is_asset=is_asset,
        current_value=data.current_value,
        value_as_of_date=data.value_as_of_date,
        notes=data.notes,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def update_account(
    db: Session, account_id: int, data: AccountUpdate
) -> Account | None:
    account = db.get(Account, account_id)
    if not account:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(account, field, value)

    db.commit()
    db.refresh(account)
    return account


def delete_account(db: Session, account_id: int) -> bool:
    account = db.get(Account, account_id)
    if not account:
        return False
    db.delete(account)
    db.commit()
    return True


def get_account_balance(
    db: Session,
    account_id: int,
    as_of_date: datetime | None = None,
    target_currency: str | None = None,
) -> float:
    """Compute the balance of an account as of a given date.

    For transactional accounts: sum of transactions.
    For non-transactional assets: latest valuation or current_value.
    If `target_currency` is set and differs from the account currency,
    the balance is converted using available FX rates.
    """
    account = db.get(Account, account_id)
    if not account:
        return 0.0

    base_ccy = target_currency or settings.base_currency
    balance = 0.0

    if account.account_type in TRANSACTIONAL_TYPES:
        # Use amount_base when available (already converted), else amount
        query = select(
            func.coalesce(
                func.sum(Transaction.amount_base),
                func.sum(Transaction.amount),
                0.0,
            )
        ).where(Transaction.account_id == account_id)

        if as_of_date:
            query = query.where(Transaction.date <= as_of_date)
        result = db.execute(query).scalar()
        balance = float(result) if result else 0.0

        # If sum is zero but we have a manual current_value, use that
        if balance == 0.0 and account.current_value is not None:
            balance = account.current_value
    else:
        # Non-transactional: use valuation history
        if as_of_date:
            valuation = db.execute(
                select(AssetValuation)
                .where(
                    AssetValuation.account_id == account_id,
                    AssetValuation.date <= as_of_date,
                )
                .order_by(AssetValuation.date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if valuation:
                balance = valuation.value
                # Convert valuation currency if needed
                if valuation.currency != base_ccy:
                    converted, _ = convert_amount(
                        db, balance, valuation.currency, base_ccy, valuation.date
                    )
                    if converted is not None:
                        return converted
                return balance

        balance = account.current_value or 0.0

    # Convert account currency to target if needed
    if account.currency != base_ccy and balance != 0.0:
        rate_date = as_of_date or datetime.now()
        converted, _ = convert_amount(
            db, balance, account.currency, base_ccy, rate_date
        )
        if converted is not None:
            return converted

    return balance


def get_accounts_grouped(
    db: Session,
    target_currency: str | None = None,
) -> dict[str, list[dict]]:
    """Return accounts grouped by type_group with balances."""
    accounts = list_accounts(db)
    groups: dict[str, list[dict]] = {}

    for acct in accounts:
        balance = get_account_balance(db, acct.id, target_currency=target_currency)
        group_name = acct.type_group
        if group_name not in groups:
            groups[group_name] = []
        groups[group_name].append({
            "account": acct,
            "balance": balance,
        })

    return groups


def get_transaction_count(db: Session, account_id: int | None = None) -> int:
    """Efficiently count transactions, optionally per account."""
    query = select(func.count(Transaction.id))
    if account_id:
        query = query.where(Transaction.account_id == account_id)
    return db.execute(query).scalar() or 0
