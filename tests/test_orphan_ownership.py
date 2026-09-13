"""Rows created without an owner are attributed — only when it is unambiguous.

The first-run claim attributes every existing row, but code paths that create
rows afterwards did not all set user_id: ten of twenty-five accounts were
found with it NULL. Scoping queries by owner would have hidden those from the
very user they belong to. With one user the fix is unambiguous; with more it
must not guess.
"""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import database as database_module
from app.database import Base
from app.models.account import Account, AccountType
from app.models.user import User


@pytest.fixture
def db(monkeypatch):
    from sqlalchemy.pool import StaticPool
    # StaticPool: the repair opens its own connection on the module engine,
    # and an in-memory database is per-connection unless shared this way.
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database_module, "engine", engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db, name):
    u = User(username=name, display_name=name, password_hash="argon2:x")
    db.add(u)
    db.flush()
    return u


def _orphan_account(db, name):
    a = Account(name=name, account_type=AccountType.CHECKING, currency="USD",
                is_asset=True)          # no user_id
    db.add(a)
    db.flush()
    return a


def _owner_of(db, account_id):
    return db.execute(
        text("SELECT user_id FROM accounts WHERE id = :i"), {"i": account_id}
    ).scalar()


def test_with_one_user_orphans_are_attributed_to_them(db):
    alice = _user(db, "alice")
    acct = _orphan_account(db, "Orphaned")
    db.commit()
    assert _owner_of(db, acct.id) is None

    fixed = database_module._assign_orphans_to_sole_user()

    assert fixed.get("accounts") == 1
    assert _owner_of(db, acct.id) == alice.id


def test_with_two_users_nothing_is_guessed(db):
    _user(db, "alice")
    _user(db, "bob")
    acct = _orphan_account(db, "Orphaned")
    db.commit()

    fixed = database_module._assign_orphans_to_sole_user()

    assert fixed == {}
    assert _owner_of(db, acct.id) is None


def test_already_owned_rows_are_left_alone(db):
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    a = _orphan_account(db, "Bobs")
    a.user_id = bob.id
    db.commit()
    bob_id, account_id = bob.id, a.id     # before bob's row is gone
    # Now remove bob so alice is sole — the row must keep bob's id, not be
    # rewritten: only NULL rows are ever touched.
    db.execute(text("DELETE FROM users WHERE id = :i"), {"i": bob_id})
    db.commit()

    database_module._assign_orphans_to_sole_user()

    assert _owner_of(db, account_id) == bob_id


def test_it_is_idempotent(db):
    _user(db, "alice")
    _orphan_account(db, "Orphaned")
    db.commit()

    first = database_module._assign_orphans_to_sole_user()
    second = database_module._assign_orphans_to_sole_user()

    assert first.get("accounts") == 1
    assert second == {}


def test_every_owned_table_is_covered():
    """The list must match the tables that actually carry user_id."""
    from sqlalchemy import inspect as sa_inspect

    have_user_id = {
        t for t in Base.metadata.tables
        if "user_id" in Base.metadata.tables[t].columns
    }
    # Tables that carry user_id as an owner (not users' own table, not
    # sessions/tokens/credentials which are auth plumbing).
    plumbing = {"users", "api_tokens", "sessions", "webauthn_credentials",
                "dismissed_scheduled_payments", "description_embeddings",
                "scheduled_match_proposals", "dismissed_duplicates",
                "deleted_transactions"}
    expected = have_user_id - plumbing
    missing = expected - set(database_module.OWNED_TABLES)

    assert not missing, f"tables with user_id not covered by the repair: {sorted(missing)}"
