"""Account CRUD and balance computation — FX aware.

Introduces AccountBalanceResult for rich balance metadata (as_of,
staleness, confidence, source used).  The legacy get_account_balance()
signature is preserved as a thin wrapper for backward compatibility.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.account import Account, AccountType, LIABILITY_TYPES
from app.models.asset_valuation import AssetValuation
from app.models.enums import BalanceTruthSource
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


@dataclass
class FxMetadata:
    fx_pair: str | None = None
    fx_rate_date: datetime | None = None
    fx_stale: bool = False


@dataclass
class AccountBalanceResult:
    value: float = 0.0
    currency: str = "USD"
    balance_as_of: datetime | None = None
    balance_source_used: str = BalanceTruthSource.TRANSACTION_SUM.value
    balance_confidence: float | None = None
    balance_stale: bool = False
    fx: FxMetadata = field(default_factory=FxMetadata)


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


def get_account_balance_rich(
    db: Session,
    account_id: int,
    as_of_date: datetime | None = None,
    target_currency: str | None = None,
) -> AccountBalanceResult:
    """Compute balance with full truth metadata.

    Dispatches based on the account's balance_truth_source:
      - transaction_sum: SUM(transactions.amount)
      - latest_statement: account.statement_balance
      - latest_valuation: most recent AssetValuation row
      - liability_balance: statement or principal balance
      - manual_mark: account.current_value
      - hybrid: transaction_sum with statement override when stale
    """
    account = db.get(Account, account_id)
    if not account:
        return AccountBalanceResult()

    base_ccy = target_currency or settings.base_currency
    result = AccountBalanceResult(currency=base_ccy)
    now = datetime.now()

    truth_source = (
        account.balance_truth_source
        or BalanceTruthSource.TRANSACTION_SUM.value
    )

    if truth_source == BalanceTruthSource.LATEST_STATEMENT.value:
        result = _balance_from_statement(account, as_of_date, now)
    elif truth_source == BalanceTruthSource.LATEST_VALUATION.value:
        result = _balance_from_valuation(db, account, as_of_date, base_ccy, now)
    elif truth_source == BalanceTruthSource.LIABILITY_BALANCE.value:
        result = _balance_from_liability(account, as_of_date, now)
    elif truth_source == BalanceTruthSource.MANUAL_MARK.value:
        result = _balance_from_manual(account, now)
    elif truth_source == BalanceTruthSource.HYBRID.value:
        result = _balance_hybrid(db, account, account_id, as_of_date, now)
    else:
        result = _balance_from_txn_sum(db, account, account_id, as_of_date, now)

    result.currency = base_ccy

    # FX conversion if needed
    if account.currency != base_ccy and result.value != 0.0:
        rate_date = as_of_date or now
        converted, rate_used = convert_amount(
            db, result.value, account.currency, base_ccy, rate_date,
        )
        if converted is not None:
            result.value = converted
            result.fx = FxMetadata(
                fx_pair=f"{account.currency}/{base_ccy}",
                fx_rate_date=rate_date,
                fx_stale=False,
            )
        else:
            try:
                from app.services.fx_rate_fetcher import sync_current_rates
                sync_current_rates(db, base=account.currency, quotes=[base_ccy])
                converted, rate_used = convert_amount(
                    db, result.value, account.currency, base_ccy, rate_date,
                )
                if converted is not None:
                    result.value = converted
                    result.fx = FxMetadata(
                        fx_pair=f"{account.currency}/{base_ccy}",
                        fx_rate_date=rate_date,
                        fx_stale=False,
                    )
            except Exception:
                result.fx.fx_stale = True

    return result


# ── Balance dispatch helpers ────────────────────────────────────────


def _balance_from_txn_sum(
    db: Session, account: Account, account_id: int,
    as_of_date: datetime | None, now: datetime,
) -> AccountBalanceResult:
    query = select(
        func.coalesce(func.sum(Transaction.amount), 0.0)
    ).where(Transaction.account_id == account_id)
    if as_of_date:
        query = query.where(Transaction.date <= as_of_date)
    raw = db.execute(query).scalar()
    balance = float(raw) if raw else 0.0

    if balance == 0.0 and account.current_value is not None:
        balance = account.current_value

    return AccountBalanceResult(
        value=balance,
        balance_as_of=as_of_date or now,
        balance_source_used=BalanceTruthSource.TRANSACTION_SUM.value,
        balance_confidence=0.8 if balance != 0.0 else 0.3,
        balance_stale=False,
    )


def _balance_from_statement(
    account: Account, as_of_date: datetime | None, now: datetime,
) -> AccountBalanceResult:
    balance = account.statement_balance or account.current_value or 0.0
    stmt_date = account.statement_balance_as_of
    stale = False
    if stmt_date and (now - stmt_date).days > 45:
        stale = True
    return AccountBalanceResult(
        value=balance,
        balance_as_of=stmt_date or as_of_date or now,
        balance_source_used=BalanceTruthSource.LATEST_STATEMENT.value,
        balance_confidence=0.9 if not stale else 0.5,
        balance_stale=stale,
    )


def _balance_from_valuation(
    db: Session, account: Account,
    as_of_date: datetime | None, base_ccy: str, now: datetime,
) -> AccountBalanceResult:
    query = select(AssetValuation).where(
        AssetValuation.account_id == account.id,
    )
    if as_of_date:
        query = query.where(AssetValuation.date <= as_of_date)
    query = query.order_by(AssetValuation.date.desc()).limit(1)
    valuation = db.execute(query).scalar_one_or_none()

    if valuation:
        stale = (now - valuation.date).days > 90
        val = valuation.value
        if valuation.currency != base_ccy:
            converted, _ = convert_amount(
                db, val, valuation.currency, base_ccy, valuation.date,
            )
            if converted is not None:
                val = converted
        return AccountBalanceResult(
            value=val,
            balance_as_of=valuation.date,
            balance_source_used=BalanceTruthSource.LATEST_VALUATION.value,
            balance_confidence=0.85 if not stale else 0.4,
            balance_stale=stale,
        )

    return AccountBalanceResult(
        value=account.current_value or 0.0,
        balance_as_of=account.value_as_of_date or now,
        balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
        balance_confidence=0.3,
        balance_stale=True,
    )


def _balance_from_liability(
    account: Account, as_of_date: datetime | None, now: datetime,
) -> AccountBalanceResult:
    source = account.liability_balance_source or "statement_balance"
    if source == "imported_principal_balance" and account.original_principal_balance:
        balance = account.original_principal_balance
    elif account.statement_balance is not None:
        balance = account.statement_balance
    else:
        balance = account.current_value or 0.0

    stmt_date = account.statement_balance_as_of
    stale = bool(account.liability_balance_stale)
    if not stale and stmt_date and (now - stmt_date).days > 45:
        stale = True

    return AccountBalanceResult(
        value=balance,
        balance_as_of=stmt_date or as_of_date or now,
        balance_source_used=BalanceTruthSource.LIABILITY_BALANCE.value,
        balance_confidence=0.85 if not stale else 0.4,
        balance_stale=stale,
    )


def _balance_from_manual(
    account: Account, now: datetime,
) -> AccountBalanceResult:
    stale = False
    if account.value_as_of_date and (now - account.value_as_of_date).days > 90:
        stale = True
    return AccountBalanceResult(
        value=account.current_value or 0.0,
        balance_as_of=account.value_as_of_date or now,
        balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
        balance_confidence=0.5 if not stale else 0.2,
        balance_stale=stale,
    )


def _balance_hybrid(
    db: Session, account: Account, account_id: int,
    as_of_date: datetime | None, now: datetime,
) -> AccountBalanceResult:
    """Transaction sum preferred; fall back to statement if txn sum is zero."""
    txn_result = _balance_from_txn_sum(db, account, account_id, as_of_date, now)
    if txn_result.value != 0.0:
        return txn_result
    if account.statement_balance is not None:
        return _balance_from_statement(account, as_of_date, now)
    return txn_result


# ── Backward-compatible thin wrapper ────────────────────────────────


def get_account_balance(
    db: Session,
    account_id: int,
    as_of_date: datetime | None = None,
    target_currency: str | None = None,
) -> float:
    """Legacy signature — returns a plain float for existing callers."""
    return get_account_balance_rich(
        db, account_id, as_of_date=as_of_date, target_currency=target_currency,
    ).value


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
