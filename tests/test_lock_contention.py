"""One long request must not lock everyone else out.

The duplicates page held a write lock for its whole 25-second run — the
embedding cache was written inside the request's own transaction while it
waited on the model — and the auth gate wrote on every request to touch the
session's last-seen time. Every other request then hit "database is locked",
the gate failed closed and sent the user to /login, and login could not write
its session either.
"""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.description_embedding import DescriptionEmbedding
from app.models.session import Session as AuthSession
from app.models.user import User
from app.services import embeddings as emb
from app.services.clock import naive_utc_now
from app.services.sessions import create_session, lookup_session


@pytest.fixture
def db():
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db):
    u = User(username="u", display_name="U", password_hash="argon2:x")
    db.add(u); db.flush()
    return u


def test_a_recently_seen_session_is_verified_without_a_write(db):
    tok = create_session(db, _user(db).id); db.commit()
    writes = []
    real_commit = db.commit
    db.commit = lambda: (writes.append(1), real_commit())[1]

    assert lookup_session(db, tok) is not None
    assert writes == []                       # seen a moment ago: nothing to record


def test_an_idle_session_is_touched(db):
    tok = create_session(db, _user(db).id); db.commit()
    row = db.execute(select(AuthSession)).scalar_one()
    row.last_seen_at = naive_utc_now() - timedelta(hours=1); db.commit()

    lookup_session(db, tok)

    assert (naive_utc_now() - row.last_seen_at).total_seconds() < 5


def test_a_locked_touch_does_not_deny_a_valid_session(db):
    """The read verified the session; a busy database only costs the touch."""
    tok = create_session(db, _user(db).id); db.commit()
    row = db.execute(select(AuthSession)).scalar_one()
    row.last_seen_at = naive_utc_now() - timedelta(hours=1); db.commit()

    def locked():
        raise OperationalError("UPDATE sessions", {}, Exception("database is locked"))
    db.commit = locked

    assert lookup_session(db, tok) is not None


def test_embedding_cache_is_committed_on_its_own_not_left_open(db, monkeypatch):
    monkeypatch.setattr(emb, "available", lambda: True)
    monkeypatch.setattr(emb, "_embed_remote", lambda texts: [[1.0, 0.0] for _ in texts])

    vectors = emb.get_vectors(db, ["Mission FCU", "Alta Vista"])

    assert len(vectors) == 2
    assert not db.new and not db.dirty          # nothing pending on the caller's transaction
    other = sessionmaker(bind=db.get_bind())()
    try:
        assert other.execute(select(DescriptionEmbedding)).scalars().all()   # visible: it was committed
    finally:
        other.close()
