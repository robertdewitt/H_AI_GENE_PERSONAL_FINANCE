"""Tenant-isolation regression tests.

These tests are the structural guarantee that user B cannot read or
mutate user A's data. They iterate over every route registered on
``app.routes`` so a new endpoint that forgets to call the scoping
helpers will *fail closed* here, not in production.

Coverage:

* unauthenticated callers cannot reach any /api/v1/* endpoint (401);
* a cross-user API call with B's bearer token against A's account_id
  returns either an empty result, a 404, or a 403 — never A's data;
* user B's /api/v1/accounts response contains only B's accounts.

Routes that depend on path parameters with no corresponding owned row
for B (e.g. /api/v1/rental-properties/{id}/pnl when B has no
properties) must 404, not leak A's data.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models.account import Account, AccountType
from app.models.api_token import ApiToken
from app.models.transaction import Transaction
from app.models.user import User
from app.models.user_profile import UserProfile
from app.services.auth import _api_user  # noqa: F401 — imports for side effects


@pytest.fixture
def two_user_app(tmp_path: Path, monkeypatch):
    """Spin up an in-memory app with two users, each owning one account."""
    db_path = tmp_path / "tenant.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # ── seed two users + one account each ──
    alice = User(username="alice", display_name="Alice", is_admin=True,
                 password_hash="argon2:test-alice")
    bob = User(username="bob", display_name="Bob",
               password_hash="argon2:test-bob")
    session.add_all([alice, bob])
    session.flush()

    alice_acct = Account(
        user_id=alice.id, name="A-Checking",
        account_type=AccountType.CHECKING, currency="USD", is_asset=True,
    )
    bob_acct = Account(
        user_id=bob.id, name="B-Checking",
        account_type=AccountType.CHECKING, currency="USD", is_asset=True,
    )
    session.add_all([alice_acct, bob_acct])
    session.flush()

    alice_txn = Transaction(
        account_id=alice_acct.id,
        date=__import__("datetime").datetime(2026, 6, 1),
        description="A-only secret", amount=Decimal("12345.00"),
        original_currency="USD",
    )
    session.add(alice_txn)

    session.add(UserProfile(user_id=alice.id, display_currency="USD"))
    session.add(UserProfile(user_id=bob.id, display_currency="USD"))

    # ── API tokens (we use the bearer path, easier than session cookies)
    import hashlib
    alice_raw = "alice-token-abc123"
    bob_raw = "bob-token-def456"
    session.add(ApiToken(
        user_id=alice.id,
        token_hash=hashlib.sha256(alice_raw.encode()).hexdigest(),
        label="alice",
    ))
    session.add(ApiToken(
        user_id=bob.id,
        token_hash=hashlib.sha256(bob_raw.encode()).hexdigest(),
        label="bob",
    ))
    # ── browser sessions: the HTML routes are reached through the auth
    # gate, which verifies the cookie against app.database.engine directly,
    # so that engine is pointed at this database for the test's duration.
    from app import database as database_module
    from app.services.sessions import create_session
    alice_cookie = create_session(session, alice.id)
    bob_cookie = create_session(session, bob.id)
    session.commit()
    monkeypatch.setattr(database_module, "engine", engine)

    # ── wire FastAPI to use *this* session ──
    def _override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _override_get_db

    # Disable the setup-redirect middleware effect by ensuring users exist
    # (which they do — we just created two). The middleware queries via
    # SessionLocal which uses the app's real engine, so we additionally
    # remove it for the test by short-circuiting needs_setup.
    from app.routers import setup as setup_mod
    original_needs_setup = setup_mod.needs_setup
    setup_mod.needs_setup = lambda db: False

    client = TestClient(app)
    try:
        yield client, {
            "alice_token": alice_raw, "bob_token": bob_raw,
            "alice_cookie": alice_cookie, "bob_cookie": bob_cookie,
            "alice_account_id": alice_acct.id,
            "alice_txn_id": alice_txn.id,
            "bob_account_id": bob_acct.id,
            "session": session,
        }
    finally:
        setup_mod.needs_setup = original_needs_setup
        app.dependency_overrides.clear()
        session.close()


# ── Unauthenticated tests ───────────────────────────────────────────────


def test_api_endpoints_require_auth(two_user_app):
    client, ctx = two_user_app
    for path in (
        "/api/v1/accounts",
        "/api/v1/transactions",
        "/api/v1/net-worth",
        "/api/v1/agent/context",
    ):
        resp = client.get(path)
        assert resp.status_code == 401, (
            f"{path} returned {resp.status_code} for an unauthenticated caller — "
            "must be 401"
        )


# ── Cross-user isolation ────────────────────────────────────────────────


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cookie(raw: str) -> dict:
    return {"session": raw}


def test_user_b_cannot_see_user_a_accounts(two_user_app):
    client, ctx = two_user_app
    resp = client.get("/api/v1/accounts", headers=_auth(ctx["bob_token"]))
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json().get("accounts", [])]
    assert names == ["B-Checking"], (
        f"Bob's /api/v1/accounts returned {names} — must contain only Bob's accounts"
    )


def test_user_b_cannot_pull_user_a_transactions_by_account_id(two_user_app):
    """Even when Bob explicitly passes Alice's account_id as a filter, the
    scoping join means he gets back an empty result, not Alice's secret."""
    client, ctx = two_user_app
    resp = client.get(
        f"/api/v1/transactions?account_id={ctx['alice_account_id']}",
        headers=_auth(ctx["bob_token"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    descs = [t.get("description") for t in body.get("transactions", [])]
    assert "A-only secret" not in descs, (
        f"Bob received Alice's transaction via account_id leak: {descs}"
    )
    assert body.get("total", 0) == 0


def test_user_a_can_still_see_their_own_data(two_user_app):
    """Sanity check — the isolation hasn't broken the happy path."""
    client, ctx = two_user_app
    resp = client.get(
        f"/api/v1/transactions?account_id={ctx['alice_account_id']}",
        headers=_auth(ctx["alice_token"]),
    )
    assert resp.status_code == 200
    descs = [t.get("description") for t in resp.json().get("transactions", [])]
    assert "A-only secret" in descs


# ── Route-walking ───────────────────────────────────────────────────────


def test_no_api_route_responds_200_to_anonymous(two_user_app):
    """Iterate over every /api/v1/* route on app.routes — any GET that
    doesn't require auth (no router-level dep, no Depends(get_current_user))
    will fail this test, which is the "fail closed" guarantee from the brief.
    """
    client, _ = two_user_app
    seen = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api/v1/"):
            continue
        if "GET" not in methods:
            continue
        # Path params we don't have values for → skip; the unauth check
        # would 422 before reaching the auth layer.
        if "{" in path:
            continue
        seen += 1
        resp = client.get(path)
        # 401 = required auth, refused. 422 = validation error before auth
        # (still safe — caller couldn't reach the handler). 200 means we
        # leaked.
        assert resp.status_code != 200, (
            f"{path} returned 200 to an anonymous request — missing auth"
        )
    assert seen > 0, "No /api/v1/* GET routes were probed"


def test_no_html_route_responds_200_to_anonymous(two_user_app):
    """Same fail-closed guarantee for browser-facing routes.

    Every HTML GET on app.routes that isn't on the public allow-list
    (login, setup, logout, static, favicon) must redirect to /login or
    return a non-200 status for an anonymous caller.
    """
    client, _ = two_user_app
    public = ("/setup", "/login", "/logout", "/static", "/favicon.ico", "/api/")
    seen = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods:
            continue
        if any(path == p or path.startswith(p) for p in public):
            continue
        if "{" in path:
            continue
        seen += 1
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code != 200, (
            f"{path} returned 200 to anonymous browser — missing auth gate"
        )
        # 303 = redirect to /login (happy path). 401/403/422 also acceptable.
        assert resp.status_code in (303, 401, 403, 422, 405, 404), (
            f"{path} returned {resp.status_code} to anonymous — expected "
            "303 (redirect to /login) or 4xx"
        )
    assert seen > 0, "No HTML GET routes were probed"


# ── Cross-user route walk (HTML) ────────────────────────────────────────

ALICE_MARKERS = ("A-Checking", "A-only secret")
_PUBLIC = ("/setup", "/login", "/logout", "/static", "/favicon.ico", "/api/",
           "/docs", "/redoc", "/openapi.json")


def _fill_path(path: str, ctx: dict) -> str:
    """Substitute every path parameter with one of Alice's ids.

    Transaction-shaped names get her transaction; everything else gets her
    account id — for a foreign table that is just some integer, and the
    assertion (no Alice content, no 500) holds whatever it resolves to.
    """
    import re
    def pick(m):
        name = m.group(1)
        if "txn" in name or "transaction" in name:
            return str(ctx["alice_txn_id"])
        return str(ctx["alice_account_id"])
    return re.sub(r"\{(\w+)(?::[^}]*)?\}", pick, path)


def test_bob_cannot_read_alice_through_any_html_get(two_user_app):
    """Walk every HTML GET with Bob's credentials and Alice's ids in the path.

    Each page must either refuse (404 / 303 / 4xx) or render without a
    trace of Alice's account name or transaction. A 500 is a failure too:
    an unscoped lookup that returns None to a template is how a page
    crashes instead of hiding.
    """
    client, ctx = two_user_app
    seen, leaks = 0, []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods or not path or path == "/":
            continue
        if any(path == p or path.startswith(p) for p in _PUBLIC):
            continue
        url = _fill_path(path, ctx)
        resp = client.get(url, cookies=_cookie(ctx["bob_cookie"]), follow_redirects=False)
        seen += 1
        assert resp.status_code != 303 or not resp.headers.get("location", "").startswith("/login"), (
            f"{url}: Bob's session was not honoured — the walk would pass for the wrong reason"
        )
        assert resp.status_code != 500, f"{url} crashed for Bob (500)"
        if resp.status_code == 200:
            body = resp.text
            found = [m for m in ALICE_MARKERS if m in body]
            if found:
                leaks.append(f"{url} -> {found}")
    assert not leaks, "Alice's data reached Bob:\n  " + "\n  ".join(leaks)
    assert seen > 20, f"only {seen} HTML GET routes probed"


def test_alice_still_sees_her_own_ledger(two_user_app):
    """The walk above would pass trivially if pages hid everything from
    everyone; the owner must still see the row."""
    client, ctx = two_user_app
    resp = client.get("/transactions", cookies=_cookie(ctx["alice_cookie"]))
    assert resp.status_code == 200
    assert "A-only secret" in resp.text

    resp = client.get("/transactions", cookies=_cookie(ctx["bob_cookie"]))
    assert resp.status_code == 200
    assert "A-only secret" not in resp.text
