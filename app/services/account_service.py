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


def get_many_account_balances_rich(
    db: Session,
    accounts: list[Account] | None = None,
    target_currency: str | None = None,
) -> dict[int, "AccountBalanceResult"]:
    """Batch balance computation for all accounts.

    For accounts using transaction_sum as their truth source (the majority),
    one GROUP BY query is issued for all of them together.  Accounts with
    other truth sources (statement, valuation, manual) have their balances
    computed from account fields in Python — no additional DB calls needed
    except for latest_valuation accounts, which share a single GROUP BY query.

    Returns {account_id: AccountBalanceResult}.
    """
    from app.models.asset_valuation import AssetValuation
    from app.models.currency_rate import CurrencyRate
    from app.services.fx_service import convert_amount

    base_ccy = target_currency or settings.base_currency
    now = datetime.now()

    if accounts is None:
        accounts = list_accounts(db)

    if not accounts:
        return {}

    # Partition by truth source
    txn_sum_ids = [
        a.id for a in accounts
        if (a.balance_truth_source or BalanceTruthSource.TRANSACTION_SUM.value)
        in (BalanceTruthSource.TRANSACTION_SUM.value, BalanceTruthSource.HYBRID.value)
    ]
    valuation_ids = [
        a.id for a in accounts
        if (a.balance_truth_source or "") == BalanceTruthSource.LATEST_VALUATION.value
    ]

    # Batch: transaction sums
    txn_sums: dict[int, float] = {}
    if txn_sum_ids:
        for row in db.execute(
            select(
                Transaction.account_id,
                func.coalesce(func.sum(Transaction.amount), 0.0).label("total"),
            )
            .where(Transaction.account_id.in_(txn_sum_ids))
            .group_by(Transaction.account_id)
        ).all():
            txn_sums[row.account_id] = float(row.total)

    # Batch: latest valuation dates
    latest_val: dict[int, tuple[datetime, float, str]] = {}
    if valuation_ids:
        for row in db.execute(
            select(
                AssetValuation.account_id,
                AssetValuation.date,
                AssetValuation.value,
                AssetValuation.currency,
            )
            .where(AssetValuation.account_id.in_(valuation_ids))
            .order_by(AssetValuation.account_id, AssetValuation.date.desc())
            .distinct(AssetValuation.account_id)
        ).all():
            latest_val[row.account_id] = (row.date, row.value, row.currency)

    # Batch: latest FX rates for all non-base currencies
    non_base_ccys = {a.currency for a in accounts if a.currency != base_ccy}
    fx_rates: dict[str, tuple[float, datetime] | None] = {}
    for ccy in non_base_ccys:
        row = db.execute(
            select(CurrencyRate.rate, CurrencyRate.date)
            .where(
                CurrencyRate.base_currency == base_ccy,
                CurrencyRate.quote_currency == ccy,
            )
            .order_by(CurrencyRate.date.desc())
            .limit(1)
        ).one_or_none()
        fx_rates[ccy] = (row.rate, row.date) if row else None

    results: dict[int, AccountBalanceResult] = {}

    for acct in accounts:
        truth_source = acct.balance_truth_source or BalanceTruthSource.TRANSACTION_SUM.value
        result: AccountBalanceResult

        if truth_source in (
            BalanceTruthSource.TRANSACTION_SUM.value,
            BalanceTruthSource.HYBRID.value,
        ):
            balance = txn_sums.get(acct.id, 0.0)
            # HYBRID fallback to statement when txn sum is zero
            if balance == 0.0 and truth_source == BalanceTruthSource.HYBRID.value:
                if acct.statement_balance is not None:
                    balance = acct.statement_balance
            if balance == 0.0 and acct.current_value is not None:
                balance = acct.current_value
            result = AccountBalanceResult(
                value=balance,
                balance_as_of=now,
                balance_source_used=truth_source,
                balance_confidence=0.8 if balance != 0.0 else 0.3,
                balance_stale=False,
                currency=base_ccy,
            )

        elif truth_source == BalanceTruthSource.LATEST_STATEMENT.value:
            balance = acct.statement_balance or acct.current_value or 0.0
            stmt_date = acct.statement_balance_as_of
            stale = bool(stmt_date and (now - stmt_date).days > 45)
            result = AccountBalanceResult(
                value=balance,
                balance_as_of=stmt_date or now,
                balance_source_used=BalanceTruthSource.LATEST_STATEMENT.value,
                balance_confidence=0.9 if not stale else 0.5,
                balance_stale=stale,
                currency=base_ccy,
            )

        elif truth_source == BalanceTruthSource.LIABILITY_BALANCE.value:
            source = acct.liability_balance_source or "statement_balance"
            if source == "imported_principal_balance" and acct.original_principal_balance:
                balance = acct.original_principal_balance
            elif acct.statement_balance is not None:
                balance = acct.statement_balance
            else:
                balance = acct.current_value or 0.0
            stmt_date = acct.statement_balance_as_of
            stale = bool(acct.liability_balance_stale) or bool(
                stmt_date and (now - stmt_date).days > 45
            )
            result = AccountBalanceResult(
                value=balance,
                balance_as_of=stmt_date or now,
                balance_source_used=BalanceTruthSource.LIABILITY_BALANCE.value,
                balance_confidence=0.85 if not stale else 0.4,
                balance_stale=stale,
                currency=base_ccy,
            )

        elif truth_source == BalanceTruthSource.MANUAL_MARK.value:
            stale = bool(
                acct.value_as_of_date and (now - acct.value_as_of_date).days > 90
            ) or acct.value_as_of_date is None
            result = AccountBalanceResult(
                value=acct.current_value or 0.0,
                balance_as_of=acct.value_as_of_date or now,
                balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
                balance_confidence=0.5 if not stale else 0.2,
                balance_stale=stale,
                currency=base_ccy,
            )

        elif truth_source == BalanceTruthSource.LATEST_VALUATION.value:
            if acct.id in latest_val:
                val_date, val_value, val_ccy = latest_val[acct.id]
                stale = (now - val_date).days > 90
                result = AccountBalanceResult(
                    value=val_value,
                    balance_as_of=val_date,
                    balance_source_used=BalanceTruthSource.LATEST_VALUATION.value,
                    balance_confidence=0.85 if not stale else 0.4,
                    balance_stale=stale,
                    currency=val_ccy,
                )
            else:
                result = AccountBalanceResult(
                    value=acct.current_value or 0.0,
                    balance_as_of=acct.value_as_of_date or now,
                    balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
                    balance_confidence=0.3,
                    balance_stale=True,
                    currency=acct.currency,
                )
        else:
            result = AccountBalanceResult(
                value=acct.current_value or 0.0,
                balance_as_of=now,
                balance_source_used=truth_source,
                balance_confidence=0.3,
                balance_stale=True,
                currency=base_ccy,
            )

        # FX conversion using pre-cached rates
        if acct.currency != base_ccy and result.value != 0.0:
            fx_entry = fx_rates.get(acct.currency)
            if fx_entry is not None:
                rate, rate_date = fx_entry
                result.value = result.value * rate
                result.fx = FxMetadata(
                    fx_pair=f"{acct.currency}/{base_ccy}",
                    fx_rate_date=rate_date,
                    fx_stale=(now - rate_date).days > 7,
                )
            else:
                result.fx = FxMetadata(
                    fx_pair=f"{acct.currency}/{base_ccy}",
                    fx_stale=True,
                )

        result.currency = base_ccy
        results[acct.id] = result

    return results


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
