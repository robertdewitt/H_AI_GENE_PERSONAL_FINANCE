"""Session cookie flags, and the passkey login sharing the password form's budget.

Both login paths set the session cookie with ``secure=False`` hard-coded,
which is harmless on plain-HTTP localhost and wrong the day the app sits
behind TLS. And only ``/login`` was rate-limited: the passkey ceremony
endpoints were public and unbounded.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.responses import Response

from app.services.sessions import SESSION_COOKIE_MAX_AGE, attach_session_cookie


# ── Cookie flags ─────────────────────────────────────────────────────────


def _request(scheme: str) -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": scheme,
        "server": ("testserver", 443 if scheme == "https" else 80),
        "client": ("testclient", 1), "path": "/login", "raw_path": b"/login",
        "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver")], "app": None,
    })


def _cookie(scheme: str) -> str:
    resp = Response()
    attach_session_cookie(resp, _request(scheme), "tok")
    return resp.headers["set-cookie"]


def test_secure_is_set_over_https():
    assert "secure" in _cookie("https").lower()


def test_secure_is_omitted_over_plain_http():
    """Localhost development must keep working."""
    assert "secure" not in _cookie("http").lower()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_httponly_and_samesite_lax_on_every_scheme(scheme):
    header = _cookie(scheme).lower()

    assert "httponly" in header
    assert "samesite=lax" in header


def test_the_cookie_carries_the_token_and_the_lifetime():
    header = _cookie("http")

    assert header.startswith("session=tok")
    assert f"Max-Age={SESSION_COOKIE_MAX_AGE}" in header


# ── Passkey login rate limit ─────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    """App wired to an empty in-memory DB and a fresh 3-attempt limiter."""
    import app.main as main_module
    import app.routers.auth_routes as auth_routes
    import app.routers.webauthn as webauthn
    from app.database import Base, get_db
    from app.services.rate_limit import RateLimiter

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    main_module.app.dependency_overrides[get_db] = _override
    limiter = RateLimiter(max_attempts=3, window_seconds=60)
    # Both routers import the limiter by name, so both must see the same
    # fresh instance — that shared instance is the point being tested.
    monkeypatch.setattr(webauthn, "login_limiter", limiter)
    monkeypatch.setattr(auth_routes, "login_limiter", limiter)
    try:
        yield TestClient(main_module.app, follow_redirects=False)
    finally:
        main_module.app.dependency_overrides.clear()


def _options(client, username):
    return client.post("/auth/webauthn/login/options", data={"username": username})


def test_passkey_options_are_rate_limited(client):
    for _ in range(3):
        assert _options(client, "alice").status_code != 429

    fourth = _options(client, "alice")

    assert fourth.status_code == 429
    assert fourth.json() == {"ok": False, "error": "Too many attempts — try again later"}


def test_passkey_verify_shares_the_same_budget(client):
    for _ in range(3):
        _options(client, "alice")

    resp = client.post(
        "/auth/webauthn/login/verify",
        json={"username": "alice", "return_to": "/", "id": "x", "response": {}},
    )

    assert resp.status_code == 429


def test_the_budget_is_per_username(client):
    """One user's attempts must not lock out another."""
    for _ in range(3):
        _options(client, "alice")

    assert _options(client, "bob").status_code != 429


def test_password_and_passkey_draw_on_one_budget(client):
    """Three passkey attempts for a name exhaust the password form for it too —
    otherwise an attacker just alternates."""
    for _ in range(3):
        _options(client, "alice")

    resp = client.post(
        "/login", data={"username": "alice", "password": "wrong", "return_to": "/"},
    )

    assert resp.status_code == 303
    assert "Too%20many" in resp.headers["location"] or "Too many" in resp.headers["location"]
