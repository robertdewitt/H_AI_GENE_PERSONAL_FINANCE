"""The payoff projection has to start from the balance the page is showing.

statement_balance was preferred as "more accurate", which held when the
computed balance was a bare transaction sum that could be missing history. A
statement-anchored balance already starts from that statement and adds the
payments made since, so preferring the raw statement projected from a figure
one or more payments out of date — and printed an Outstanding Balance that
contradicted the Current Balance card directly above it.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.database import Base
from app.models.account import Account, AccountType
from app.models.enums import BalanceTruthSource
from app.models.transaction import Transaction
from app.routers.accounts import account_detail


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _request(path):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "server": ("testserver", 80), "client": ("testclient", 1),
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [(b"host", b"testserver")], "app": None,
    })


def _mortgage(db, truth=BalanceTruthSource.HYBRID.value):
    acct = Account(
        name="Villa Mortgage", account_type=AccountType.MORTGAGE,
        currency="USD", is_asset=False, balance_truth_source=truth,
        interest_rate=0.025, monthly_payment=Decimal("3245.24"),
    )
    acct.statement_balance = Decimal("188697.72")
    acct.statement_balance_as_of = datetime.combine(
        date.today() - timedelta(days=25), datetime.min.time(),
    )
    db.add(acct)
    db.flush()
    return acct


def _payoff(db, acct):
    return account_detail(
        _request(f"/accounts/{acct.id}"), acct.id, db=db,
    ).context["mortgage_payoff"]


def test_projection_starts_from_the_live_balance_not_the_statement(db):
    acct = _mortgage(db)
    # A payment made after the statement — the ledger knows, the statement doesn't.
    db.add(Transaction(
        account_id=acct.id,
        date=datetime.combine(date.today() - timedelta(days=3), datetime.min.time()),
        description="Payments Irregular", amount=Decimal("3290.00"),
        original_currency="USD",
    ))
    db.commit()

    payoff = _payoff(db, acct)

    assert payoff["current_balance"] == pytest.approx(185407.72, abs=0.01)


def test_projection_matches_the_balance_card(db):
    """The two figures sit one above the other on the page; they must agree."""
    acct = _mortgage(db)
    db.add(Transaction(
        account_id=acct.id,
        date=datetime.combine(date.today() - timedelta(days=3), datetime.min.time()),
        description="Payments Irregular", amount=Decimal("3290.00"),
        original_currency="USD",
    ))
    db.commit()
    ctx = account_detail(
        _request(f"/accounts/{acct.id}"), acct.id, db=db,
    ).context

    assert ctx["mortgage_payoff"]["current_balance"] == pytest.approx(
        abs(float(ctx["balance"])), abs=0.01,
    )


def test_statement_still_used_when_there_is_no_computed_balance(db):
    """An account with no ledger at all still projects from its statement."""
    acct = _mortgage(db, truth=BalanceTruthSource.LATEST_STATEMENT.value)
    db.commit()

    payoff = _payoff(db, acct)

    assert payoff["current_balance"] == pytest.approx(188697.72, abs=0.01)


def test_schedule_amortises_down_to_zero(db):
    acct = _mortgage(db)
    db.commit()

    payoff = _payoff(db, acct)

    assert payoff["schedule"][-1]["balance"] == 0.0
    # Principal rises and interest falls as the balance comes down.
    first, last = payoff["schedule"][0], payoff["schedule"][-1]
    assert last["principal"] > first["principal"]
    assert last["interest"] < first["interest"]
    assert payoff["payoff_date"] == last["label"]


def test_no_projection_when_the_payment_cannot_cover_the_interest(db):
    acct = _mortgage(db)
    acct.monthly_payment = Decimal("100.00")   # below the monthly interest
    db.commit()

    assert _payoff(db, acct) is None
