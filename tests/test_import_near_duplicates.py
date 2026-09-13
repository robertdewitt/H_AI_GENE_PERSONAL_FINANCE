"""The same transaction, worded differently by two sources, imports once.

A PDF statement recorded a payment as "Payment Thank You Bill Pay Service";
the CSV export of the same month truncated it to "Payment Thank You Bill Pa".
The source-row fingerprint sees different bytes and the exact key sees
different text, so both let it through — five overlapping exports of one card
leaked a second copy of a £4,738 payment that way.
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.import_service import _wording_matches_existing, import_transactions

MAPPING = {"date": "Date", "description": "Description", "amount": "Amount"}


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _card(db):
    a = Account(name="Card", account_type=AccountType.CREDIT_CARD,
                currency="USD", is_asset=False)
    db.add(a)
    db.flush()
    return a


def _csv(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines))
    return str(p)


def _import(db, acct, path):
    return import_transactions(
        db, acct.id, path, column_mapping=MAPPING, account_currency="USD",
        is_liability=True, dayfirst=False,
    )


def _rows(db, acct):
    return db.execute(
        select(Transaction).where(Transaction.account_id == acct.id)
        .order_by(Transaction.date, Transaction.id)
    ).scalars().all()


# ── The matcher on its own ───────────────────────────────────────────────


def test_a_truncated_description_matches_the_full_one():
    assert _wording_matches_existing(
        "Payment Thank You Bill Pa",
        ["payment thank you bill pay service"],
    )


def test_different_merchants_on_the_same_day_and_amount_do_not_match():
    """Two £7.10 charges on one day — Blank Street and TfL — are two charges."""
    assert not _wording_matches_existing(
        "BLANK STREET UK LIMITED London",
        ["tfl travel charge tfl gov uk"],
    )


def test_a_different_reference_number_is_a_different_payment():
    """The case the user corrected by hand: two Faster Payments of the same
    amount on the same day, references one digit apart. Similarity scores
    them 0.96 — the reference has to win."""
    assert not _wording_matches_existing(
        "Faster Payment IPBINB4001744 RO",
        ["faster payment ipbinb4001743 ro"],
    )


def test_the_same_reference_number_worded_differently_is_one_payment():
    assert _wording_matches_existing(
        "Faster Payment IPBINB4001744",
        ["faster payment ipbinb4001744 robert dewitt"],
    )


def test_a_truncation_just_under_the_ratio_is_still_caught():
    """0.847 by ratio; a prefix by construction."""
    assert _wording_matches_existing(
        "Payment Thank You Bill Pa",
        ["payment thank you bill pay service"],
    )


def test_a_short_prefix_is_not_enough_on_its_own():
    """"pay" is a prefix of a great many things."""
    assert not _wording_matches_existing("Pay", ["payment thank you bill pay service"])


# ── Through the importer ─────────────────────────────────────────────────


def test_a_reworded_repeat_from_a_second_source_is_not_imported_twice(db, tmp_path):
    acct = _card(db)
    _import(db, acct, _csv(tmp_path, "statement.csv", [
        "Date,Description,Amount",
        "04/20/2026,Payment Thank You Bill Pay Service,4738.19",
    ]))
    assert len(_rows(db, acct)) == 1

    batch = _import(db, acct, _csv(tmp_path, "export.csv", [
        "Date,Description,Amount",
        "04/20/2026,Payment Thank You Bill Pa,4738.19",
    ]))

    assert len(_rows(db, acct)) == 1
    assert batch.row_count == 0


def test_a_genuinely_different_charge_on_the_same_day_is_kept(db, tmp_path):
    acct = _card(db)
    _import(db, acct, _csv(tmp_path, "a.csv", [
        "Date,Description,Amount",
        "04/01/2026,BLANK STREET UK LIMITED London,-7.10",
    ]))

    _import(db, acct, _csv(tmp_path, "b.csv", [
        "Date,Description,Amount",
        "04/01/2026,TFL TRAVEL CHARGE TFL.GOV.UK,-7.10",
    ]))

    assert [r.description for r in _rows(db, acct)] == [
        "BLANK STREET UK LIMITED London", "TFL TRAVEL CHARGE TFL.GOV.UK",
    ]


def test_a_legitimate_repeat_within_one_file_is_still_kept(db, tmp_path):
    """The third layer only looks at rows already on the ledger, never at
    the file being imported — two identical parking charges stay two."""
    acct = _card(db)

    _import(db, acct, _csv(tmp_path, "day.csv", [
        "Date,Description,Amount",
        "01/01/2026,SAN DIEGO PARKING,-3.75",
        "01/01/2026,SAN DIEGO PARKING,-3.75",
    ]))

    assert len(_rows(db, acct)) == 2


def test_the_count_of_skipped_duplicates_is_reported(db, tmp_path):
    acct = _card(db)
    _import(db, acct, _csv(tmp_path, "one.csv", [
        "Date,Description,Amount",
        "04/20/2026,Payment Thank You Bill Pay Service,4738.19",
        "04/21/2026,OCADO HATFIELD,-42.00",
    ]))

    batch = _import(db, acct, _csv(tmp_path, "two.csv", [
        "Date,Description,Amount",
        "04/20/2026,Payment Thank You Bill Pa,4738.19",
        "04/21/2026,OCADO HATFIELD LTD,-42.00",
        "04/22/2026,NEW SHOP,-5.00",
    ]))

    assert batch.row_count == 1
    assert getattr(batch, "_duplicates_skipped", 0) == 2
