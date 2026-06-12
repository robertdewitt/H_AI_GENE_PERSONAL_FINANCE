"""Account CRUD and balance computation — FX aware.

Introduces AccountBalanceResult for rich balance metadata (as_of,
staleness, confidence, source used).  The legacy get_account_balance()
signature is preserved as a thin wrapper for backward compatibility.
"""
from dataclasses import dataclass, field
from datetime import datetime
from app.services.clock import naive_utc_now
from decimal import Decimal

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
    value: Decimal = Decimal("0.00")
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
    now = naive_utc_now()

    truth_source = (
        account.balance_truth_source
        or BalanceTruthSource.TRANSACTION_SUM.value
    )

    if truth_source == BalanceTruthSource.LATEST_STATEMENT.value:
        # Prefer the most recent LiabilityBalanceSnapshot (accurate per-statement
        # record) over account.statement_balance which can fall behind if statements
        # are imported out of chronological order.
        from app.models.snapshots import LiabilityBalanceSnapshot
        snap_query = (
            select(LiabilityBalanceSnapshot)
            .where(LiabilityBalanceSnapshot.account_id == account_id)
            .order_by(LiabilityBalanceSnapshot.as_of_date.desc())
            .limit(1)
        )
        if as_of_date:
            snap_query = snap_query.where(
                LiabilityBalanceSnapshot.as_of_date <= as_of_date
            )
        latest_snap = db.execute(snap_query).scalar_one_or_none()
        if latest_snap is not None:
            stale = (now - latest_snap.as_of_date).days > 45
            result = AccountBalanceResult(
                value=latest_snap.value_native,
                balance_as_of=latest_snap.as_of_date,
                balance_source_used=BalanceTruthSource.LATEST_STATEMENT.value,
                balance_confidence=0.95 if not stale else 0.5,
                balance_stale=stale,
            )
        else:
            result = _balance_from_statement(account, as_of_date, now)
    elif truth_source == BalanceTruthSource.LATEST_VALUATION.value:
        result = _balance_from_valuation(db, account, as_of_date, base_ccy, now)
    elif truth_source == BalanceTruthSource.LIABILITY_BALANCE.value:
        result = _balance_from_liability(account, as_of_date, now)
    elif truth_source == BalanceTruthSource.MANUAL_MARK.value:
        if as_of_date is not None:
            # For historical queries (time series), prefer a dated valuation so the
            # chart reflects actual changes in asset value over time.
            result = _balance_from_valuation(db, account, as_of_date, base_ccy, now)
        else:
            result = _balance_from_manual(account, now)
    elif truth_source == BalanceTruthSource.HYBRID.value:
        result = _balance_hybrid(db, account, account_id, as_of_date, now)
    else:
        result = _balance_from_txn_sum(db, account, account_id, as_of_date, now)

    result.currency = base_ccy

    # FX conversion if needed
    if account.currency != base_ccy and result.value != Decimal("0.00"):
        rate_date = as_of_date or now
        converted, rate_used = convert_amount(
            db, result.value, account.currency, base_ccy, rate_date,
        )
        if converted is not None:
            result.value = Decimal(str(converted))
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
                    result.value = Decimal(str(converted))
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
    # Prefer the bank's own running balance when transactions include balance_after.
    # This gives accurate balances even when transaction history is incomplete.
    if not as_of_date:
        bal_after_row = db.execute(
            select(Transaction.balance_after, Transaction.date)
            .where(
                Transaction.account_id == account_id,
                Transaction.balance_after.isnot(None),
            )
            .order_by(Transaction.date.desc(), Transaction.id.desc())
            .limit(1)
        ).one_or_none()
        if bal_after_row and bal_after_row.balance_after is not None:
            return AccountBalanceResult(
                value=Decimal(str(bal_after_row.balance_after)),
                balance_as_of=bal_after_row.date,
                balance_source_used="latest_balance_after",
                balance_confidence=0.92,
                balance_stale=False,
            )

    query = select(
        func.coalesce(func.sum(Transaction.amount), 0)
    ).where(Transaction.account_id == account_id)
    if as_of_date:
        query = query.where(Transaction.date <= as_of_date)
    raw = db.execute(query).scalar()
    balance = raw or Decimal("0.00")

    if balance == Decimal("0.00") and account.current_value is not None:
        balance = account.current_value

    return AccountBalanceResult(
        value=balance,
        balance_as_of=as_of_date or now,
        balance_source_used=BalanceTruthSource.TRANSACTION_SUM.value,
        balance_confidence=0.8 if balance != Decimal("0.00") else 0.3,
        balance_stale=False,
    )


def _balance_from_statement(
    account: Account, as_of_date: datetime | None, now: datetime,
) -> AccountBalanceResult:
    balance = account.statement_balance or account.current_value or Decimal("0.00")
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
            # For current-balance display use today's rate; for historical queries
            # use the rate at the as_of_date so the time-series chart is accurate.
            fx_date = as_of_date or now
            converted, _ = convert_amount(
                db, val, valuation.currency, base_ccy, fx_date,
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
        value=account.current_value or Decimal("0.00"),
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
        balance = account.current_value or Decimal("0.00")

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
        value=account.current_value or Decimal("0.00"),
        balance_as_of=account.value_as_of_date or now,
        balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
        balance_confidence=0.5 if not stale else 0.2,
        balance_stale=stale,
    )


def _balance_hybrid(
    db: Session, account: Account, account_id: int,
    as_of_date: datetime | None, now: datetime,
) -> AccountBalanceResult:
    """Statement-anchored balance: statement_balance + sum(txns since statement_date).

    This gives accurate balances even with incomplete transaction history.
    Falls back to pure transaction_sum when no statement balance is set.
    """
    if account.statement_balance is not None and account.statement_balance_as_of is not None:
        stmt_bal = account.statement_balance
        stmt_date = account.statement_balance_as_of
        cutoff = as_of_date or now
        delta = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0))
            .where(
                Transaction.account_id == account_id,
                Transaction.date > stmt_date,
                Transaction.date <= cutoff,
            )
        ).scalar() or Decimal("0.00")
        balance = stmt_bal + Decimal(str(delta))
        stale = (now - stmt_date).days > 45
        return AccountBalanceResult(
            value=balance,
            balance_as_of=cutoff,
            balance_source_used="statement_anchored",
            balance_confidence=0.9 if not stale else 0.6,
            balance_stale=stale,
        )

    # No statement balance — fall back to transaction_sum (with balance_after enhancement)
    return _balance_from_txn_sum(db, account, account_id, as_of_date, now)


# ── Backward-compatible thin wrapper ────────────────────────────────


def get_account_balance(
    db: Session,
    account_id: int,
    as_of_date: datetime | None = None,
    target_currency: str | None = None,
) -> Decimal:
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
    now = naive_utc_now()

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
    latest_statement_ids = [
        a.id for a in accounts
        if (a.balance_truth_source or "") == BalanceTruthSource.LATEST_STATEMENT.value
    ]

    # Batch: latest LiabilityBalanceSnapshot per account for LATEST_STATEMENT sources
    # (matches single-account path, which prefers snapshots over account.statement_balance).
    latest_liab_snap: dict[int, tuple[Decimal, datetime]] = {}
    if latest_statement_ids:
        from app.models.snapshots import LiabilityBalanceSnapshot
        snap_sq = (
            select(
                LiabilityBalanceSnapshot.account_id,
                func.max(LiabilityBalanceSnapshot.as_of_date).label("max_date"),
            )
            .where(LiabilityBalanceSnapshot.account_id.in_(latest_statement_ids))
            .group_by(LiabilityBalanceSnapshot.account_id)
            .subquery()
        )
        for row in db.execute(
            select(
                LiabilityBalanceSnapshot.account_id,
                LiabilityBalanceSnapshot.value_native,
                LiabilityBalanceSnapshot.as_of_date,
            )
            .join(
                snap_sq,
                (LiabilityBalanceSnapshot.account_id == snap_sq.c.account_id)
                & (LiabilityBalanceSnapshot.as_of_date == snap_sq.c.max_date),
            )
        ).all():
            latest_liab_snap[row.account_id] = (
                Decimal(str(row.value_native)), row.as_of_date,
            )

    # Batch: latest balance_after per account (most accurate when available)
    latest_bal_after: dict[int, tuple[Decimal, datetime]] = {}
    if txn_sum_ids:
        # Subquery: latest transaction id with balance_after per account
        sq = (
            select(
                Transaction.account_id,
                func.max(Transaction.id).label("max_id"),
            )
            .where(
                Transaction.account_id.in_(txn_sum_ids),
                Transaction.balance_after.isnot(None),
            )
            .group_by(Transaction.account_id)
            .subquery()
        )
        for row in db.execute(
            select(Transaction.account_id, Transaction.balance_after, Transaction.date)
            .join(sq, (Transaction.account_id == sq.c.account_id) & (Transaction.id == sq.c.max_id))
        ).all():
            if row.balance_after is not None:
                latest_bal_after[row.account_id] = (
                    Decimal(str(row.balance_after)), row.date
                )

    # Batch: transaction sums (used when balance_after not available)
    # Separate hybrid accounts that have a statement anchor from pure transaction_sum ones.
    hybrid_anchored: dict[int, tuple[Decimal, datetime]] = {}  # id -> (stmt_bal, stmt_date)
    for a in accounts:
        if (
            (a.balance_truth_source or BalanceTruthSource.TRANSACTION_SUM.value) == BalanceTruthSource.HYBRID.value
            and a.statement_balance is not None
            and a.statement_balance_as_of is not None
            and a.id not in latest_bal_after
        ):
            hybrid_anchored[a.id] = (a.statement_balance, a.statement_balance_as_of)

    txn_sum_ids_without_bal_after = [
        i for i in txn_sum_ids
        if i not in latest_bal_after and i not in hybrid_anchored
    ]
    txn_sums: dict[int, Decimal] = {}
    if txn_sum_ids_without_bal_after:
        for row in db.execute(
            select(
                Transaction.account_id,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            )
            .where(Transaction.account_id.in_(txn_sum_ids_without_bal_after))
            .group_by(Transaction.account_id)
        ).all():
            txn_sums[row.account_id] = row.total

    # Batch: delta sums for statement-anchored accounts (since each statement_date)
    # Fetch all transactions >= min(statement_date), then filter per-account in Python.
    hybrid_deltas: dict[int, Decimal] = {}
    if hybrid_anchored:
        min_stmt_date = min(d for _, d in hybrid_anchored.values())
        for row in db.execute(
            select(
                Transaction.account_id,
                Transaction.date,
                Transaction.amount,
            )
            .where(
                Transaction.account_id.in_(list(hybrid_anchored.keys())),
                Transaction.date > min_stmt_date,
            )
        ).all():
            _, stmt_date = hybrid_anchored[row.account_id]
            if row.date > stmt_date:
                hybrid_deltas[row.account_id] = (
                    hybrid_deltas.get(row.account_id, Decimal("0.00"))
                    + Decimal(str(row.amount))
                )

    # Batch: latest valuation per account (max date, then max id to break ties)
    latest_val: dict[int, tuple[datetime, Decimal, str]] = {}
    if valuation_ids:
        max_date_sq = (
            select(
                AssetValuation.account_id,
                func.max(AssetValuation.date).label("max_date"),
            )
            .where(AssetValuation.account_id.in_(valuation_ids))
            .group_by(AssetValuation.account_id)
            .subquery()
        )
        max_id_sq = (
            select(
                AssetValuation.account_id,
                func.max(AssetValuation.id).label("max_id"),
            )
            .join(
                max_date_sq,
                (AssetValuation.account_id == max_date_sq.c.account_id)
                & (AssetValuation.date == max_date_sq.c.max_date),
            )
            .group_by(AssetValuation.account_id)
            .subquery()
        )
        for row in db.execute(
            select(
                AssetValuation.account_id,
                AssetValuation.date,
                AssetValuation.value,
                AssetValuation.currency,
            )
            .join(
                max_id_sq,
                (AssetValuation.account_id == max_id_sq.c.account_id)
                & (AssetValuation.id == max_id_sq.c.max_id),
            )
        ).all():
            latest_val[row.account_id] = (row.date, row.value, row.currency)

    # Batch: latest FX rates for all non-base currencies.
    # We need the rate that converts account currency → base_ccy, i.e.
    # base_currency=acct.currency, quote_currency=base_ccy (e.g. EUR→USD).
    # Fallback: if only the inverse is stored (base_ccy→acct.currency), use 1/rate.
    # Also include currencies from asset valuations (which may differ from account.currency,
    # e.g. a USD-denominated IRA account with GBP-valued IBKR statements).
    non_base_ccys = {a.currency for a in accounts if a.currency != base_ccy}
    non_base_ccys |= {ccy for _, __, ccy in latest_val.values() if ccy != base_ccy}
    fx_rates: dict[str, tuple[float, datetime] | None] = {}
    for ccy in non_base_ccys:
        # Preferred direction: ccy → base_ccy (e.g. EUR→USD)
        row = db.execute(
            select(CurrencyRate.rate, CurrencyRate.date)
            .where(
                CurrencyRate.base_currency == ccy,
                CurrencyRate.quote_currency == base_ccy,
            )
            .order_by(CurrencyRate.date.desc())
            .limit(1)
        ).one_or_none()
        if row:
            fx_rates[ccy] = (row.rate, row.date)
        else:
            # Fallback: inverse direction stored (base_ccy → ccy)
            inv = db.execute(
                select(CurrencyRate.rate, CurrencyRate.date)
                .where(
                    CurrencyRate.base_currency == base_ccy,
                    CurrencyRate.quote_currency == ccy,
                )
                .order_by(CurrencyRate.date.desc())
                .limit(1)
            ).one_or_none()
            fx_rates[ccy] = (1.0 / inv.rate, inv.date) if inv and inv.rate else None

    results: dict[int, AccountBalanceResult] = {}

    for acct in accounts:
        truth_source = acct.balance_truth_source or BalanceTruthSource.TRANSACTION_SUM.value
        result: AccountBalanceResult

        if truth_source in (
            BalanceTruthSource.TRANSACTION_SUM.value,
            BalanceTruthSource.HYBRID.value,
        ):
            # Prefer bank's running balance (balance_after) over sum of transactions
            if acct.id in latest_bal_after:
                bal_val, bal_date = latest_bal_after[acct.id]
                result = AccountBalanceResult(
                    value=bal_val,
                    balance_as_of=bal_date,
                    balance_source_used="latest_balance_after",
                    balance_confidence=0.92,
                    balance_stale=False,
                    currency=acct.currency,
                )
            elif acct.id in hybrid_anchored:
                stmt_bal, stmt_date = hybrid_anchored[acct.id]
                delta = hybrid_deltas.get(acct.id, Decimal("0.00"))
                stale = (now - stmt_date).days > 45
                result = AccountBalanceResult(
                    value=stmt_bal + delta,
                    balance_as_of=now,
                    balance_source_used="statement_anchored",
                    balance_confidence=0.9 if not stale else 0.6,
                    balance_stale=stale,
                    currency=acct.currency,
                )
            else:
                balance = txn_sums.get(acct.id, Decimal("0.00"))
                if balance == Decimal("0.00") and acct.current_value is not None:
                    balance = acct.current_value
                result = AccountBalanceResult(
                    value=balance,
                    balance_as_of=now,
                    balance_source_used=truth_source,
                    balance_confidence=0.8 if balance != Decimal("0.00") else 0.3,
                    balance_stale=False,
                    currency=acct.currency,
                )

        elif truth_source == BalanceTruthSource.LATEST_STATEMENT.value:
            # Prefer the most recent LiabilityBalanceSnapshot (accurate per-statement
            # record) over account.statement_balance which can fall behind if statements
            # are imported out of chronological order.
            if acct.id in latest_liab_snap:
                snap_val, snap_date = latest_liab_snap[acct.id]
                stale = (now - snap_date).days > 45
                result = AccountBalanceResult(
                    value=snap_val,
                    balance_as_of=snap_date,
                    balance_source_used=BalanceTruthSource.LATEST_STATEMENT.value,
                    balance_confidence=0.95 if not stale else 0.5,
                    balance_stale=stale,
                    currency=acct.currency,
                )
            else:
                balance = acct.statement_balance or acct.current_value or Decimal("0.00")
                stmt_date = acct.statement_balance_as_of
                stale = bool(stmt_date and (now - stmt_date).days > 45)
                result = AccountBalanceResult(
                    value=balance,
                    balance_as_of=stmt_date or now,
                    balance_source_used=BalanceTruthSource.LATEST_STATEMENT.value,
                    balance_confidence=0.9 if not stale else 0.5,
                    balance_stale=stale,
                    currency=acct.currency,
                )

        elif truth_source == BalanceTruthSource.LIABILITY_BALANCE.value:
            source = acct.liability_balance_source or "statement_balance"
            if source == "imported_principal_balance" and acct.original_principal_balance:
                balance = acct.original_principal_balance
            elif acct.statement_balance is not None:
                balance = acct.statement_balance
            else:
                balance = acct.current_value or Decimal("0.00")
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
                currency=acct.currency,
            )

        elif truth_source == BalanceTruthSource.MANUAL_MARK.value:
            stale = bool(
                acct.value_as_of_date and (now - acct.value_as_of_date).days > 90
            ) or acct.value_as_of_date is None
            result = AccountBalanceResult(
                value=acct.current_value or Decimal("0.00"),
                balance_as_of=acct.value_as_of_date or now,
                balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
                balance_confidence=0.5 if not stale else 0.2,
                balance_stale=stale,
                currency=acct.currency,
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
                    value=acct.current_value or Decimal("0.00"),
                    balance_as_of=acct.value_as_of_date or now,
                    balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
                    balance_confidence=0.3,
                    balance_stale=True,
                    currency=acct.currency,
                )
        else:
            result = AccountBalanceResult(
                value=acct.current_value or Decimal("0.00"),
                balance_as_of=now,
                balance_source_used=truth_source,
                balance_confidence=0.3,
                balance_stale=True,
                currency=acct.currency,
            )

        # FX conversion using pre-cached rates.
        # result.currency now honestly reflects the currency of result.value
        # (account.currency for transaction/statement-based sources, valuation
        # currency for LATEST_VALUATION). Convert when it differs from base.
        effective_ccy = result.currency or acct.currency
        if effective_ccy != base_ccy and result.value != Decimal("0.00"):
            fx_entry = fx_rates.get(effective_ccy)
            if fx_entry is not None:
                rate, rate_date = fx_entry
                result.value = result.value * Decimal(str(rate))
                result.fx = FxMetadata(
                    fx_pair=f"{effective_ccy}/{base_ccy}",
                    fx_rate_date=rate_date,
                    fx_stale=(now - rate_date).days > 7,
                )
            else:
                result.fx = FxMetadata(
                    fx_pair=f"{effective_ccy}/{base_ccy}",
                    fx_stale=True,
                )

        result.currency = base_ccy
        results[acct.id] = result

    return results


def get_many_account_balances_series(
    db: Session,
    accounts: list[Account] | None = None,
    snapshot_dates: list[datetime] | None = None,
    target_currency: str | None = None,
) -> dict[datetime, dict[int, AccountBalanceResult]]:
    """Compute per-account balances at multiple snapshot dates in bounded SQL.

    Returns ``{snapshot_date: {account_id: AccountBalanceResult}}``.

    Implementation: pre-loads every input the truth-source dispatch needs
    (raw transactions, valuations, liability snapshots, currency rates) in
    a fixed number of queries, then answers every (account, date) cell
    from the in-memory tables. This keeps the cost flat at ~5-8 SQL
    statements regardless of how many months are requested.

    Falls back to per-date :func:`get_many_account_balances_rich` when no
    dates are supplied or there are no accounts.
    """
    from app.models.asset_valuation import AssetValuation
    from app.models.currency_rate import CurrencyRate
    from app.models.snapshots import LiabilityBalanceSnapshot

    if accounts is None:
        accounts = list_accounts(db)
    if not accounts or not snapshot_dates:
        return {}

    base_ccy = target_currency or settings.base_currency
    now = naive_utc_now()
    acct_ids = [a.id for a in accounts]
    sorted_dates = sorted(snapshot_dates)

    # ── 1) Transactions (used for TRANSACTION_SUM, HYBRID, latest_balance_after) ──
    # One scan, grouped per-account in Python so we can answer any snapshot date.
    txn_rows = db.execute(
        select(
            Transaction.account_id,
            Transaction.date,
            Transaction.amount,
            Transaction.balance_after,
            Transaction.id,
        )
        .where(Transaction.account_id.in_(acct_ids))
        .order_by(Transaction.account_id, Transaction.date, Transaction.id)
    ).all()

    txns_by_account: dict[int, list[tuple]] = {}
    for row in txn_rows:
        txns_by_account.setdefault(row.account_id, []).append(
            (row.date, Decimal(str(row.amount or 0)),
             Decimal(str(row.balance_after)) if row.balance_after is not None else None,
             row.id)
        )

    # ── 2) Latest AssetValuation per (account, snapshot_date) ──
    val_rows = db.execute(
        select(
            AssetValuation.account_id,
            AssetValuation.date,
            AssetValuation.value,
            AssetValuation.currency,
        )
        .where(AssetValuation.account_id.in_(acct_ids))
        .order_by(AssetValuation.account_id, AssetValuation.date)
    ).all()
    vals_by_account: dict[int, list[tuple]] = {}
    for r in val_rows:
        vals_by_account.setdefault(r.account_id, []).append(
            (r.date, Decimal(str(r.value or 0)), r.currency)
        )

    # ── 3) Latest LiabilityBalanceSnapshot per (account, snapshot_date) ──
    snap_rows = db.execute(
        select(
            LiabilityBalanceSnapshot.account_id,
            LiabilityBalanceSnapshot.as_of_date,
            LiabilityBalanceSnapshot.value_native,
        )
        .where(LiabilityBalanceSnapshot.account_id.in_(acct_ids))
        .order_by(LiabilityBalanceSnapshot.account_id, LiabilityBalanceSnapshot.as_of_date)
    ).all()
    snaps_by_account: dict[int, list[tuple]] = {}
    for r in snap_rows:
        snaps_by_account.setdefault(r.account_id, []).append(
            (r.as_of_date, Decimal(str(r.value_native or 0)))
        )

    # ── 4) FX rates (one fetch per non-base currency we'll need) ──
    non_base_ccys = {a.currency for a in accounts if a.currency and a.currency != base_ccy}
    non_base_ccys |= {
        ccy for plist in vals_by_account.values() for _, _, ccy in plist
        if ccy and ccy != base_ccy
    }
    fx_history: dict[str, list[tuple[datetime, float]]] = {}
    if non_base_ccys:
        rate_rows = db.execute(
            select(
                CurrencyRate.base_currency, CurrencyRate.quote_currency,
                CurrencyRate.rate, CurrencyRate.date,
            )
            .where(
                ((CurrencyRate.base_currency.in_(non_base_ccys))
                 & (CurrencyRate.quote_currency == base_ccy))
                | ((CurrencyRate.base_currency == base_ccy)
                   & (CurrencyRate.quote_currency.in_(non_base_ccys)))
            )
            .order_by(CurrencyRate.date)
        ).all()
        for r in rate_rows:
            # Forward direction: ccy → base. Inverse: invert the rate.
            if r.base_currency == base_ccy:
                ccy = r.quote_currency
                rate = (1.0 / r.rate) if r.rate else 0.0
            else:
                ccy = r.base_currency
                rate = float(r.rate or 0.0)
            fx_history.setdefault(ccy, []).append((r.date, rate))

    def _fx_rate_at(ccy: str, when: datetime) -> tuple[float, datetime] | None:
        rows = fx_history.get(ccy)
        if not rows:
            return None
        last: tuple[datetime, float] | None = None
        for rate_date, rate in rows:
            if rate_date <= when:
                last = (rate_date, rate)
            else:
                break
        if last is None:
            # Fall back to earliest rate if no on-or-before sample exists.
            rate_date, rate = rows[0]
            return (rate, rate_date)
        rate_date, rate = last
        return (rate, rate_date)

    def _txn_sum_at(account_id: int, when: datetime) -> Decimal:
        total = Decimal("0.00")
        for d, amt, _bal_after, _tid in txns_by_account.get(account_id, ()):
            if d <= when:
                total += amt
            else:
                break
        return total

    def _latest_bal_after_at(account_id: int, when: datetime) -> tuple[Decimal, datetime] | None:
        result: tuple[Decimal, datetime] | None = None
        for d, _amt, bal_after, _tid in txns_by_account.get(account_id, ()):
            if d > when:
                break
            if bal_after is not None:
                result = (bal_after, d)
        return result

    def _latest_val_at(account_id: int, when: datetime) -> tuple[datetime, Decimal, str] | None:
        result = None
        for d, v, c in vals_by_account.get(account_id, ()):
            if d > when:
                break
            result = (d, v, c)
        return result

    def _latest_snap_at(account_id: int, when: datetime) -> tuple[Decimal, datetime] | None:
        result = None
        for d, v in snaps_by_account.get(account_id, ()):
            if d > when:
                break
            result = (v, d)
        return result

    # ── Compose per-date results ──
    out: dict[datetime, dict[int, AccountBalanceResult]] = {}

    for snapshot_date in sorted_dates:
        per_acct: dict[int, AccountBalanceResult] = {}
        for acct in accounts:
            truth_source = (
                acct.balance_truth_source
                or BalanceTruthSource.TRANSACTION_SUM.value
            )
            result: AccountBalanceResult

            if truth_source in (
                BalanceTruthSource.TRANSACTION_SUM.value,
                BalanceTruthSource.HYBRID.value,
            ):
                bal_after = _latest_bal_after_at(acct.id, snapshot_date)
                if bal_after is not None:
                    val, val_date = bal_after
                    result = AccountBalanceResult(
                        value=val,
                        balance_as_of=val_date,
                        balance_source_used="latest_balance_after",
                        balance_confidence=0.92,
                        balance_stale=False,
                        currency=acct.currency,
                    )
                elif (
                    truth_source == BalanceTruthSource.HYBRID.value
                    and acct.statement_balance is not None
                    and acct.statement_balance_as_of is not None
                ):
                    stmt_bal = Decimal(str(acct.statement_balance))
                    stmt_date = acct.statement_balance_as_of
                    # Delta = txns in (stmt_date, snapshot_date]
                    delta = Decimal("0.00")
                    for d, amt, _b, _tid in txns_by_account.get(acct.id, ()):
                        if d <= stmt_date:
                            continue
                        if d > snapshot_date:
                            break
                        delta += amt
                    stale = (snapshot_date - stmt_date).days > 45
                    result = AccountBalanceResult(
                        value=stmt_bal + delta,
                        balance_as_of=snapshot_date,
                        balance_source_used="statement_anchored",
                        balance_confidence=0.9 if not stale else 0.6,
                        balance_stale=stale,
                        currency=acct.currency,
                    )
                else:
                    balance = _txn_sum_at(acct.id, snapshot_date)
                    if balance == Decimal("0.00") and acct.current_value is not None:
                        balance = Decimal(str(acct.current_value))
                    result = AccountBalanceResult(
                        value=balance,
                        balance_as_of=snapshot_date,
                        balance_source_used=truth_source,
                        balance_confidence=0.8 if balance != Decimal("0.00") else 0.3,
                        balance_stale=False,
                        currency=acct.currency,
                    )

            elif truth_source == BalanceTruthSource.LATEST_STATEMENT.value:
                snap = _latest_snap_at(acct.id, snapshot_date)
                if snap is not None:
                    snap_val, snap_date = snap
                    stale = (snapshot_date - snap_date).days > 45
                    result = AccountBalanceResult(
                        value=snap_val,
                        balance_as_of=snap_date,
                        balance_source_used=BalanceTruthSource.LATEST_STATEMENT.value,
                        balance_confidence=0.95 if not stale else 0.5,
                        balance_stale=stale,
                        currency=acct.currency,
                    )
                else:
                    balance = (
                        Decimal(str(acct.statement_balance))
                        if acct.statement_balance is not None
                        else (Decimal(str(acct.current_value))
                              if acct.current_value is not None else Decimal("0.00"))
                    )
                    stmt_date = acct.statement_balance_as_of
                    stale = bool(stmt_date and (snapshot_date - stmt_date).days > 45)
                    result = AccountBalanceResult(
                        value=balance,
                        balance_as_of=stmt_date or snapshot_date,
                        balance_source_used=BalanceTruthSource.LATEST_STATEMENT.value,
                        balance_confidence=0.9 if not stale else 0.5,
                        balance_stale=stale,
                        currency=acct.currency,
                    )

            elif truth_source == BalanceTruthSource.LIABILITY_BALANCE.value:
                source = acct.liability_balance_source or "statement_balance"
                if source == "imported_principal_balance" and acct.original_principal_balance:
                    balance = Decimal(str(acct.original_principal_balance))
                elif acct.statement_balance is not None:
                    balance = Decimal(str(acct.statement_balance))
                else:
                    balance = (
                        Decimal(str(acct.current_value))
                        if acct.current_value is not None else Decimal("0.00")
                    )
                stmt_date = acct.statement_balance_as_of
                stale = bool(acct.liability_balance_stale) or bool(
                    stmt_date and (snapshot_date - stmt_date).days > 45
                )
                result = AccountBalanceResult(
                    value=balance,
                    balance_as_of=stmt_date or snapshot_date,
                    balance_source_used=BalanceTruthSource.LIABILITY_BALANCE.value,
                    balance_confidence=0.85 if not stale else 0.4,
                    balance_stale=stale,
                    currency=acct.currency,
                )

            elif truth_source == BalanceTruthSource.MANUAL_MARK.value:
                # For time-series, prefer a dated valuation if one exists.
                val = _latest_val_at(acct.id, snapshot_date)
                if val is not None:
                    val_date, val_value, val_ccy = val
                    stale = (snapshot_date - val_date).days > 90
                    result = AccountBalanceResult(
                        value=val_value,
                        balance_as_of=val_date,
                        balance_source_used=BalanceTruthSource.LATEST_VALUATION.value,
                        balance_confidence=0.85 if not stale else 0.4,
                        balance_stale=stale,
                        currency=val_ccy or acct.currency,
                    )
                else:
                    stale = bool(
                        acct.value_as_of_date
                        and (snapshot_date - acct.value_as_of_date).days > 90
                    ) or acct.value_as_of_date is None
                    result = AccountBalanceResult(
                        value=Decimal(str(acct.current_value)) if acct.current_value is not None else Decimal("0.00"),
                        balance_as_of=acct.value_as_of_date or snapshot_date,
                        balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
                        balance_confidence=0.5 if not stale else 0.2,
                        balance_stale=stale,
                        currency=acct.currency,
                    )

            elif truth_source == BalanceTruthSource.LATEST_VALUATION.value:
                val = _latest_val_at(acct.id, snapshot_date)
                if val is not None:
                    val_date, val_value, val_ccy = val
                    stale = (snapshot_date - val_date).days > 90
                    result = AccountBalanceResult(
                        value=val_value,
                        balance_as_of=val_date,
                        balance_source_used=BalanceTruthSource.LATEST_VALUATION.value,
                        balance_confidence=0.85 if not stale else 0.4,
                        balance_stale=stale,
                        currency=val_ccy or acct.currency,
                    )
                else:
                    result = AccountBalanceResult(
                        value=Decimal(str(acct.current_value)) if acct.current_value is not None else Decimal("0.00"),
                        balance_as_of=acct.value_as_of_date or snapshot_date,
                        balance_source_used=BalanceTruthSource.MANUAL_MARK.value,
                        balance_confidence=0.3,
                        balance_stale=True,
                        currency=acct.currency,
                    )
            else:
                result = AccountBalanceResult(
                    value=Decimal(str(acct.current_value)) if acct.current_value is not None else Decimal("0.00"),
                    balance_as_of=snapshot_date,
                    balance_source_used=truth_source,
                    balance_confidence=0.3,
                    balance_stale=True,
                    currency=acct.currency,
                )

            # ── FX conversion to base_ccy (uses on-or-before rate) ──
            effective_ccy = result.currency or acct.currency
            if effective_ccy and effective_ccy != base_ccy and result.value != Decimal("0.00"):
                fx = _fx_rate_at(effective_ccy, snapshot_date)
                if fx is not None:
                    rate, rate_date = fx
                    result.value = result.value * Decimal(str(rate))
                    result.fx = FxMetadata(
                        fx_pair=f"{effective_ccy}/{base_ccy}",
                        fx_rate_date=rate_date,
                        fx_stale=(snapshot_date - rate_date).days > 7,
                    )
                else:
                    result.fx = FxMetadata(
                        fx_pair=f"{effective_ccy}/{base_ccy}",
                        fx_stale=True,
                    )

            result.currency = base_ccy
            per_acct[acct.id] = result

        out[snapshot_date] = per_acct

    return out


def get_accounts_grouped(
    db: Session,
    target_currency: str | None = None,
) -> dict[str, list[dict]]:
    """Return accounts grouped by type_group with balances."""
    accounts = list_accounts(db)
    balances = get_many_account_balances_rich(db, accounts=accounts, target_currency=target_currency)
    groups: dict[str, list[dict]] = {}

    for acct in accounts:
        result = balances.get(acct.id) or AccountBalanceResult()
        group_name = acct.type_group
        if group_name not in groups:
            groups[group_name] = []
        groups[group_name].append({
            "account": acct,
            "balance": result.value,
            "balance_source": result.balance_source_used,
            "balance_stale": result.balance_stale,
            "balance_confidence": result.balance_confidence,
        })

    return groups


def get_transaction_count(db: Session, account_id: int | None = None) -> int:
    """Efficiently count transactions, optionally per account."""
    query = select(func.count(Transaction.id))
    if account_id:
        query = query.where(Transaction.account_id == account_id)
    return db.execute(query).scalar() or 0
