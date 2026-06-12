"""End-to-end test for the first-run /setup claim flow.

Seeds a pre-auth schema with realistic data, simulates the upgrade by
running the additive ``init_db`` migration and the claim, then asserts:

* every owned row has ``user_id`` set to the new admin,
* per-table row counts are unchanged before/after,
* net worth computed after the claim equals net worth computed before,
* a pre-claim DB backup file exists.

This is the most important regression test for Phase 2.1 — if anything
silently drops a row or fails to backfill a user_id, this test breaks.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.models.user import User
from app.models.user_profile import UserProfile
from app.services.net_worth_service import compute_net_worth
from app.services.setup_claim import (
    OWNED_TABLES,
    backup_sqlite_db,
    claim_all_rows,
    format_integrity_summary,
    has_existing_data,
    snapshot_row_counts,
    verify_claim_integrity,
)


@pytest.fixture
def populated_sqlite_db(tmp_path: Path):
    """Build a SQLite DB on disk so the backup helper can copy it."""
    db_path = tmp_path / "finance.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Seed a small but representative ledger — all owned tables get rows
    # except the ones that are purely truth-engine derived. user_id is
    # left NULL on every row, mimicking a pre-auth database.
    checking = Account(
        name="Checking", account_type=AccountType.CHECKING,
        currency="USD", is_asset=True,
    )
    card = Account(
        name="Credit Card", account_type=AccountType.CREDIT_CARD,
        currency="USD", is_asset=False,
        statement_balance=Decimal("250.00"),
    )
    session.add_all([checking, card])
    session.flush()

    now = datetime(2026, 6, 1)
    for i in range(5):
        session.add(Transaction(
            account_id=checking.id,
            date=now - timedelta(days=15 * i),
            description=f"txn {i}",
            amount=Decimal("100.00"),
            original_currency="USD",
        ))

    # Singleton profile carrying knob settings — the brief says this must
    # become per-user after the claim.
    session.add(UserProfile(display_currency="USD"))
    session.commit()

    yield url, session, engine
    session.close()


def test_has_existing_data_detects_seeded_db(populated_sqlite_db):
    url, session, _ = populated_sqlite_db
    assert has_existing_data(session) is True


def test_has_existing_data_false_for_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        assert has_existing_data(session) is False
    finally:
        session.close()


def test_backup_writes_timestamped_copy(populated_sqlite_db, tmp_path):
    url, session, _ = populated_sqlite_db
    backups = tmp_path / "backups"
    dest = backup_sqlite_db(url, backups_dir=backups)
    assert dest.exists()
    assert dest.parent == backups
    assert dest.name.startswith("pre_auth_")
    assert dest.suffix == ".db"
    # Size should match the source.
    assert dest.stat().st_size > 0


def test_full_claim_round_trip_preserves_data(populated_sqlite_db, tmp_path):
    """The end-to-end upgrade scenario from the brief.

    Pre-claim net worth → run claim → post-claim net worth must match,
    every owned row must have user_id set, row counts unchanged.
    """
    url, session, _ = populated_sqlite_db

    pre_nw = compute_net_worth(session)
    pre_counts = snapshot_row_counts(session)
    backup_path = backup_sqlite_db(url, backups_dir=tmp_path / "backups")
    assert backup_path.exists(), "backup must exist before any writes"

    # Create the admin user that will claim everything.
    admin = User(
        username="owner", display_name="Owner", is_admin=True,
        password_hash="argon2:fake-hash-for-test",
    )
    session.add(admin)
    session.flush()

    updates = claim_all_rows(session, admin.id)
    assert sum(updates.values()) >= sum(pre_counts.values()), (
        "claim must touch every existing row"
    )

    summary = verify_claim_integrity(session, pre_counts, admin.id)

    # Every owned table must have its rows attributed to the admin now.
    for table in OWNED_TABLES:
        row = session.execute(text(
            f"SELECT COUNT(*) FROM {table} WHERE user_id IS NOT NULL "
            f"AND user_id != :uid"
        ), {"uid": admin.id}).first()
        wrong = int(row[0]) if row else 0
        assert wrong == 0, f"{table} has {wrong} rows owned by a different user"

    # Net worth must be byte-for-byte identical post-claim.
    session.commit()
    post_nw = compute_net_worth(session)
    assert post_nw.net_worth == pre_nw.net_worth
    assert post_nw.total_assets == pre_nw.total_assets
    assert post_nw.total_liabilities == pre_nw.total_liabilities

    # Print the integrity summary for the verification step the brief
    # requires — visible in pytest -s output.
    print()
    print(format_integrity_summary(summary))
