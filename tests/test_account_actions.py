"""Account-page POST actions actually execute.

These endpoints are thin, but each one carries its own local imports, so a
missing name only shows up when the function is called. Invoking each here
catches that class of bug before it reaches a page.
"""
import pytest
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.instrument import Instrument, PositionLot


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _owner(db):
    """The user every account in these tests belongs to; routes now demand one."""
    from app.models.user import User
    from sqlalchemy import select as _sel
    u = db.execute(_sel(User).where(User.username == "owner")).scalar_one_or_none()
    if u is None:
        u = User(username="owner", display_name="Owner", password_hash="argon2:x")
        db.add(u); db.flush()
    return u


def _brokerage(db):
    a = Account(user_id=_owner(db).id, name="IBKR", account_type=AccountType.ROTH_IRA,
                currency="USD", is_asset=True)
    db.add(a)
    db.flush()
    return a


def test_refresh_prices_with_no_holdings_is_handled(db):
    from app.routers.accounts import account_refresh_prices

    acct = _brokerage(db)
    resp = account_refresh_prices(acct.id, db=db, user=_owner(db))
    assert resp.status_code == 303
    assert f"/accounts/{acct.id}?priced=0" in resp.headers["location"]


def test_refresh_prices_rejects_non_market_account(db):
    from app.routers.accounts import account_refresh_prices

    loan = Account(user_id=_owner(db).id, name="Car Loan", account_type=AccountType.LOAN,
                   currency="USD", is_asset=False)
    db.add(loan)
    db.flush()
    resp = account_refresh_prices(loan.id, db=db, user=_owner(db))
    assert resp.status_code == 404


def test_refresh_prices_runs_for_held_symbols(db, monkeypatch):
    """The happy path executes end to end with the price feed stubbed out."""
    import app.services.price_service as ps
    from app.routers.accounts import account_refresh_prices

    acct = _brokerage(db)
    inst = Instrument(symbol="AAPL", name="Apple", currency="USD",
                      asset_class="equity")
    db.add(inst)
    db.flush()
    db.add(PositionLot(account_id=acct.id, instrument_id=inst.id, quantity=10.0,
                       as_of_date=datetime(2026, 1, 1), source="ibkr"))
    db.flush()

    monkeypatch.setattr(
        ps, "get_current_prices",
        lambda symbols, db=None: ({s: 100.0 for s in symbols},
                                  {s: datetime(2026, 1, 2) for s in symbols}, True),
    )
    resp = account_refresh_prices(acct.id, db=db, user=_owner(db))
    assert resp.status_code == 303
    assert "priced=1" in resp.headers["location"]
    assert "live=1" in resp.headers["location"]


def test_pension_lots_are_not_price_refreshed(db, monkeypatch):
    """Pension units are statement-priced — they must not hit the market feed."""
    import app.services.price_service as ps
    from app.routers.accounts import account_refresh_prices

    acct = _brokerage(db)
    fund = Instrument(symbol="EPA:FUND", name="Fund", currency="GBP",
                      asset_class="pension_fund")
    db.add(fund)
    db.flush()
    db.add(PositionLot(account_id=acct.id, instrument_id=fund.id, quantity=5.0,
                       as_of_date=datetime(2026, 1, 1), source="epa_pension"))
    db.flush()

    seen: list[list[str]] = []

    def _spy(symbols, db=None):
        seen.append(list(symbols))
        return {}, {}, False

    monkeypatch.setattr(ps, "get_current_prices", _spy)
    resp = account_refresh_prices(acct.id, db=db, user=_owner(db))
    assert resp.status_code == 303
    # No market symbols to price, so the feed is never called.
    assert seen == []
    assert "priced=0" in resp.headers["location"]


def test_close_and_reopen_endpoints_execute(db):
    from app.routers.accounts import account_close, account_reopen

    acct = _brokerage(db)
    resp = account_close(acct.id, closed_at="2026-04-23", reason="done",
                         zero_balance=False, db=db, user=_owner(db))
    assert resp.status_code == 303
    assert acct.closed_at == date(2026, 4, 23)

    resp = account_reopen(acct.id, db=db, user=_owner(db))
    assert resp.status_code == 303
    assert acct.closed_at is None


def test_close_rejects_a_malformed_date(db):
    from app.routers.accounts import account_close

    acct = _brokerage(db)
    resp = account_close(acct.id, closed_at="23/04/2026", reason="",
                         zero_balance=False, db=db, user=_owner(db))
    assert resp.status_code == 303
    assert "close_err=date" in resp.headers["location"]
    assert acct.closed_at is None   # nothing written on a bad date
