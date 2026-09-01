"""Deleting a scheduled payment has to actually stick.

Both the recurring detector and the statement importer rebuild scheduled
payments from history, so a delete with no tombstone silently reappears.
"""
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.dismissed_scheduled_payment import (
    DismissedScheduledPayment,
    normalize_description,
)
from app.models.scheduled_payment import ScheduledPayment
from app.services.scheduled_dismissal import (
    dismiss,
    dismissed_keys,
    is_dismissed,
    list_dismissed,
    restore,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _account(db):
    a = Account(name="Amex", account_type=AccountType.CREDIT_CARD,
                currency="GBP", is_asset=False)
    db.add(a)
    db.flush()
    return a


def _payment(db, acct, desc="NETFLIX.COM  LONDON"):
    p = ScheduledPayment(
        account_id=acct.id, description=desc, amount=Decimal("-15.99"),
        amount_type="fixed", currency="GBP", frequency="monthly",
        next_due_date=date(2026, 9, 1), source="auto_detected", active=True,
    )
    db.add(p)
    db.flush()
    return p


def test_normalize_collapses_whitespace_and_case():
    assert normalize_description("SPOTIFY UK      LONDON") == "spotify uk london"
    assert normalize_description("  Spotify UK London ") == "spotify uk london"
    assert normalize_description(None) == ""


def test_delete_records_a_tombstone(db):
    from app.routers.scheduled_payments import scheduled_delete

    acct = _account(db)
    p = _payment(db, acct)
    resp = scheduled_delete(p.id, db=db)
    assert resp.status_code == 303

    assert db.get(ScheduledPayment, p.id) is None
    assert is_dismissed(db, acct.id, "NETFLIX.COM  LONDON") is True


def test_dismissal_matches_despite_spacing_differences(db):
    """The same payee arriving with different padding must still be blocked."""
    acct = _account(db)
    dismiss(db, acct.id, "SPOTIFY UK              LONDON")
    assert is_dismissed(db, acct.id, "SPOTIFY UK LONDON") is True
    assert is_dismissed(db, acct.id, "spotify uk london") is True


def test_dismissal_is_scoped_to_its_account(db):
    acct = _account(db)
    other = Account(name="Visa", account_type=AccountType.CREDIT_CARD,
                    currency="GBP", is_asset=False)
    db.add(other)
    db.flush()

    dismiss(db, acct.id, "NETFLIX")
    assert is_dismissed(db, acct.id, "NETFLIX") is True
    assert is_dismissed(db, other.id, "NETFLIX") is False


def test_dismiss_is_idempotent(db):
    acct = _account(db)
    dismiss(db, acct.id, "NETFLIX")
    dismiss(db, acct.id, "netflix")   # same key
    rows = db.execute(select(DismissedScheduledPayment)).scalars().all()
    assert len(rows) == 1


def test_blank_description_is_never_dismissed(db):
    """A blank key would match everything — refuse to store one."""
    acct = _account(db)
    assert dismiss(db, acct.id, "   ") is None
    assert dismiss(db, acct.id, None) is None
    assert is_dismissed(db, acct.id, "anything at all") is False


def test_detector_suggestions_exclude_dismissed(db):
    """The filter the detect page applies must drop dismissed suggestions."""
    acct = _account(db)
    dismiss(db, acct.id, "NETFLIX.COM  LONDON")

    suggestions = [
        {"account_id": acct.id, "description": "NETFLIX.COM LONDON"},
        {"account_id": acct.id, "description": "OCADO HATFIELD"},
    ]
    blocked = dismissed_keys(db)
    remaining = [
        s for s in suggestions
        if (s["account_id"], normalize_description(s["description"])) not in blocked
    ]
    assert [s["description"] for s in remaining] == ["OCADO HATFIELD"]


def test_restore_lets_it_be_detected_again(db):
    from app.routers.scheduled_payments import scheduled_restore_dismissed

    acct = _account(db)
    dismiss(db, acct.id, "NETFLIX")
    row = list_dismissed(db)[0]

    resp = scheduled_restore_dismissed(row.id, db=db)
    assert resp.status_code == 303
    assert is_dismissed(db, acct.id, "NETFLIX") is False


def test_restore_of_missing_row_is_harmless(db):
    assert restore(db, 99999) is False


def test_bulk_delete_removes_and_tombstones_each(db):
    from app.routers.scheduled_payments import scheduled_bulk_delete

    acct = _account(db)
    ids = [_payment(db, acct, f"PAYEE {i}").id for i in range(3)]
    keep = _payment(db, acct, "KEEP ME")

    resp = scheduled_bulk_delete(payment_ids=[str(i) for i in ids], db=db)
    assert resp.status_code == 303
    assert "deleted=3" in resp.headers["location"]

    for i in ids:
        assert db.get(ScheduledPayment, i) is None
    assert db.get(ScheduledPayment, keep.id) is not None
    for i in range(3):
        assert is_dismissed(db, acct.id, f"PAYEE {i}") is True
    assert is_dismissed(db, acct.id, "KEEP ME") is False


def test_bulk_delete_ignores_malformed_and_missing_ids(db):
    from app.routers.scheduled_payments import scheduled_bulk_delete

    acct = _account(db)
    p = _payment(db, acct, "REAL ONE")
    resp = scheduled_bulk_delete(
        payment_ids=["", "not-a-number", "99999", str(p.id)], db=db,
    )
    assert resp.status_code == 303
    assert "deleted=1" in resp.headers["location"]
    assert db.get(ScheduledPayment, p.id) is None


def test_bulk_delete_with_nothing_selected(db):
    from app.routers.scheduled_payments import scheduled_bulk_delete

    resp = scheduled_bulk_delete(payment_ids=[], db=db)
    assert resp.status_code == 303
    assert "deleted=0" in resp.headers["location"]
