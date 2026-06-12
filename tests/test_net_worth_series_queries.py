"""Query-count regression test for the batched net-worth series.

Asserts that ``compute_net_worth_series`` for a 24-month window across a
realistic account fleet issues a *bounded* number of SQL statements. The
acceptance criterion in the Phase 1 brief is < 10 — we use that as the
hard ceiling and a tighter target (~8) as the practical budget.

If this test regresses, somebody re-introduced an O(months) or
O(months × accounts) query inside the series helper.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.asset_valuation import AssetValuation
from app.models.currency_rate import CurrencyRate
from app.models.enums import BalanceTruthSource
from app.models.snapshots import LiabilityBalanceSnapshot
from app.models.transaction import Transaction
from app.services.net_worth_service import compute_net_worth_series


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session, engine
    session.close()


def _seed(db):
    """Seed a small but diverse account fleet covering each truth source."""
    now = datetime(2026, 6, 1)
    accounts = [
        Account(name="Checking USD", account_type=AccountType.CHECKING,
                currency="USD", is_asset=True,
                balance_truth_source=BalanceTruthSource.TRANSACTION_SUM.value),
        Account(name="Checking GBP", account_type=AccountType.CHECKING,
                currency="GBP", is_asset=True,
                balance_truth_source=BalanceTruthSource.TRANSACTION_SUM.value),
        Account(name="Credit Card", account_type=AccountType.CREDIT_CARD,
                currency="USD", is_asset=False,
                balance_truth_source=BalanceTruthSource.LATEST_STATEMENT.value,
                statement_balance=Decimal("1000.00"),
                statement_balance_as_of=now - timedelta(days=20)),
        Account(name="Mortgage", account_type=AccountType.MORTGAGE,
                currency="USD", is_asset=False,
                balance_truth_source=BalanceTruthSource.LIABILITY_BALANCE.value,
                statement_balance=Decimal("250000.00"),
                statement_balance_as_of=now - timedelta(days=15)),
        Account(name="House", account_type=AccountType.REAL_ESTATE,
                currency="USD", is_asset=True,
                balance_truth_source=BalanceTruthSource.LATEST_VALUATION.value,
                current_value=Decimal("500000.00"),
                value_as_of_date=now - timedelta(days=60)),
        Account(name="Pension", account_type=AccountType.PENSION,
                currency="GBP", is_asset=True,
                balance_truth_source=BalanceTruthSource.MANUAL_MARK.value,
                current_value=Decimal("150000.00"),
                value_as_of_date=now - timedelta(days=10)),
    ]
    for a in accounts:
        db.add(a)
    db.flush()

    # Sprinkle transactions across the last 24 months for the bank accounts.
    for acct in accounts[:2]:
        for i in range(24):
            db.add(Transaction(
                account_id=acct.id,
                date=now - timedelta(days=15 * i),
                description=f"txn {i}",
                amount=Decimal("100.00"),
                original_currency=acct.currency,
            ))

    # One valuation snapshot on the house mid-period
    db.add(AssetValuation(
        account_id=accounts[4].id,
        date=now - timedelta(days=200),
        value=Decimal("485000.00"),
        currency="USD",
        source="manual",
    ))

    # One liability snapshot on the credit card
    db.add(LiabilityBalanceSnapshot(
        account_id=accounts[2].id,
        as_of_date=now - timedelta(days=40),
        value_native=Decimal("950.00"),
        value_base=Decimal("950.00"),
        currency="USD",
        source="statement",
        confidence=0.95,
        stale_flag=False,
    ))

    # GBP -> USD FX rate (one row is enough for the helper to find it).
    db.add(CurrencyRate(
        base_currency="GBP",
        quote_currency="USD",
        rate=1.25,
        date=now - timedelta(days=5),
        source="test",
    ))

    db.commit()


def test_series_is_bounded_in_query_count(db):
    session, engine = db
    _seed(session)

    counter = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        # Ignore SQLAlchemy's reflection / pragma probes — only count real
        # SELECTs the helper would issue against the data tables.
        s = statement.lstrip().lower()
        if not s.startswith(("select", "with")):
            return
        counter["n"] += 1

    series = compute_net_worth_series(session, months=24, target_currency="USD")

    # 24-month range produces ~25 snapshots; the brief's ceiling is < 10
    # SQL statements regardless of months requested.
    assert len(series.snapshots) >= 24, (
        f"expected ~25 monthly snapshots, got {len(series.snapshots)}"
    )
    assert counter["n"] < 10, (
        f"series helper issued {counter['n']} SELECTs — must stay below 10 "
        "regardless of months requested"
    )


def test_series_endpoint_does_not_regress_to_per_account_loop(db):
    """Doubling the month window must not (anywhere close to) double the SQL count."""
    session, engine = db
    _seed(session)

    counts: dict[int, int] = {}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        s = statement.lstrip().lower()
        if not s.startswith(("select", "with")):
            return
        counts.setdefault("current", 0)
        counts["current"] += 1

    counts["current"] = 0
    compute_net_worth_series(session, months=6, target_currency="USD")
    six = counts["current"]

    counts["current"] = 0
    compute_net_worth_series(session, months=24, target_currency="USD")
    twentyfour = counts["current"]

    # Allow a small additive overhead but anything close to 4x is a regression.
    assert twentyfour <= six + 2, (
        f"24-month series issued {twentyfour} SELECTs vs {six} for 6 months — "
        "the helper has gone back to per-month/per-account queries"
    )
