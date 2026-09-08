"""The ledger and every uploaded statement must be owner-only on disk.

The database stores source rows verbatim — statement columns such as
"Account #" and "Card Member" — and uploads/ holds the statements themselves.
Both were created world-readable, so any local user or process could read the
lot. init_db now tightens them on every start.
"""
import os
import stat
from pathlib import Path

import pytest

from app import database as database_module
from app.config import settings


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def loose_tree(tmp_path, monkeypatch):
    """A data/ and uploads/ tree with the permissions the app used to leave."""
    saved_umask = os.umask(0o022)          # make the loose modes actually land
    try:
        data = tmp_path / "data"
        data.mkdir(mode=0o755)
        db = data / "finance.db"
        db.write_text("not really sqlite")
        (data / "finance.db-wal").write_text("")
        uploads = tmp_path / "uploads"
        uploads.mkdir(mode=0o755)
        user_dir = uploads / "1"
        user_dir.mkdir(mode=0o755)
        statement = user_dir / "statement.pdf"
        statement.write_text("%PDF")
        for p in (db, data / "finance.db-wal", statement):
            os.chmod(p, 0o644)
        for d in (data, uploads, user_dir):
            os.chmod(d, 0o755)

        monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
        monkeypatch.setattr(settings, "upload_dir", str(uploads))
        yield {
            "data": data, "db": db, "wal": data / "finance.db-wal",
            "uploads": uploads, "user_dir": user_dir, "statement": statement,
        }
    finally:
        os.umask(saved_umask)


def test_the_loose_fixture_really_is_loose(loose_tree):
    """Establishes the premise, so a passing hardening test means something."""
    assert _mode(loose_tree["db"]) == 0o644
    assert _mode(loose_tree["data"]) == 0o755
    assert _mode(loose_tree["statement"]) == 0o644


def test_hardening_makes_the_ledger_owner_only(loose_tree):
    database_module._harden_at_rest()

    assert _mode(loose_tree["data"]) == 0o700
    assert _mode(loose_tree["db"]) == 0o600
    assert _mode(loose_tree["wal"]) == 0o600


def test_hardening_covers_the_whole_uploads_tree(loose_tree):
    """Not just the root — the per-user directory and the statement in it."""
    database_module._harden_at_rest()

    assert _mode(loose_tree["uploads"]) == 0o700
    assert _mode(loose_tree["user_dir"]) == 0o700
    assert _mode(loose_tree["statement"]) == 0o600


def test_files_created_afterwards_are_owner_only_by_default(loose_tree):
    """The umask is set so anything the process writes later — WAL, a new
    upload, .env — does not start out world-readable."""
    database_module._harden_at_rest()

    fresh = loose_tree["user_dir"] / "later.csv"
    fresh.write_text("a,b")

    assert _mode(fresh) == 0o600


def test_hardening_is_idempotent_and_tolerates_missing_files(loose_tree):
    loose_tree["wal"].unlink()

    database_module._harden_at_rest()
    database_module._harden_at_rest()      # second run: nothing to do, no error

    assert _mode(loose_tree["db"]) == 0o600


def test_a_non_sqlite_url_only_touches_uploads(loose_tree, monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", "postgresql://u:p@localhost/finance",
    )

    database_module._harden_at_rest()

    assert _mode(loose_tree["uploads"]) == 0o700
    assert _mode(loose_tree["db"]) == 0o644          # untouched — not ours
