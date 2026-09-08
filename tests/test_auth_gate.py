"""The auth gate must fail closed.

The middleware wrapped its session lookup in ``except Exception: pass`` and
then fell through to the protected route. Any database error — SQLite's
"database is locked" occurs under ordinary use — therefore let a request
carrying an arbitrary ``session`` cookie straight in. An unverifiable session
is no session, and these tests hold the gate to that.
"""
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.services.sessions as sessions_module
from app.routers import setup as setup_router


@pytest.fixture
def client(monkeypatch):
    # Pretend first-run setup is done so the gate reaches the session check.
    monkeypatch.setattr(setup_router, "needs_setup", lambda db: False)
    return TestClient(main_module.app, follow_redirects=False)


def _raise(*_args, **_kwargs):
    raise RuntimeError("database is locked")


def test_a_failing_session_lookup_denies_a_request_with_a_cookie(client, monkeypatch):
    """The exact case that used to pass: lookup raises, cookie is garbage."""
    monkeypatch.setattr(sessions_module, "lookup_session", _raise)

    resp = client.get("/accounts", cookies={"session": "not-a-real-token"})

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_a_failing_session_lookup_denies_a_request_without_a_cookie(client, monkeypatch):
    monkeypatch.setattr(sessions_module, "lookup_session", _raise)

    resp = client.get("/accounts")

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_a_failing_setup_check_also_denies(client, monkeypatch):
    """needs_setup runs inside the same try; it raising must not open the gate."""
    monkeypatch.setattr(setup_router, "needs_setup", _raise)

    resp = client.get("/accounts", cookies={"session": "anything"})

    assert resp.status_code == 303


def test_a_bad_cookie_is_denied_when_the_database_is_fine(client, monkeypatch):
    monkeypatch.setattr(sessions_module, "lookup_session", lambda db, raw: None)

    resp = client.get("/accounts", cookies={"session": "not-a-real-token"})

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_a_valid_session_is_let_through(client, monkeypatch):
    """Failing closed must not mean failing always."""
    monkeypatch.setattr(sessions_module, "lookup_session", lambda db, raw: object())

    resp = client.get("/accounts", cookies={"session": "valid"})

    assert resp.status_code == 200


def test_public_paths_stay_reachable_while_the_database_is_down(client, monkeypatch):
    """The user has to be able to reach the login page to fix anything."""
    monkeypatch.setattr(sessions_module, "lookup_session", _raise)

    resp = client.get("/login")

    assert resp.status_code != 303 or not resp.headers.get("location", "").startswith("/login?return_to")


def test_the_denial_preserves_where_the_user_was_going(client, monkeypatch):
    monkeypatch.setattr(sessions_module, "lookup_session", lambda db, raw: None)

    resp = client.get("/accounts/8?forecast_months=6")

    assert resp.headers["location"] == "/login?return_to=/accounts/8?forecast_months=6"
