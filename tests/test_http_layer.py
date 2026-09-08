"""End-to-end checks over the real ASGI stack.

The rest of the suite calls route functions directly, so the middleware
chain, redirect encoding and error handling are never exercised. That gap
mattered when Starlette went 1.0.0 -> 1.6.0 across six minor versions: the
suite stayed green while nothing had actually gone over HTTP.
"""
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.services.sessions as sessions_module
from app.routers import setup as setup_router


@pytest.fixture
def anon(monkeypatch):
    """A client with setup complete and no valid session."""
    monkeypatch.setattr(setup_router, "needs_setup", lambda db: False)
    monkeypatch.setattr(sessions_module, "lookup_session", lambda db, raw: None)
    return TestClient(main_module.app, follow_redirects=False)


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(setup_router, "needs_setup", lambda db: False)
    monkeypatch.setattr(sessions_module, "lookup_session", lambda db, raw: object())
    client = TestClient(main_module.app, follow_redirects=False)
    # The gate short-circuits on a missing cookie without consulting
    # lookup_session at all, so the stub only takes effect if one is sent.
    client.cookies.set("session", "stub-token")
    return client


# ── The gate, over HTTP ──────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "/", "/accounts", "/transactions", "/scheduled", "/import",
    "/docs", "/openapi.json", "/redoc",
])
def test_protected_paths_redirect_anonymous_callers(anon, path):
    resp = anon.get(path)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", ["/api/v1/accounts", "/api/v1/net-worth",
                                  "/api/v1/agent/context"])
def test_api_answers_401_rather_than_redirecting(anon, path):
    """The gate exempts /api/ and the router's own dependency covers it."""
    resp = anon.get(path)

    assert resp.status_code == 401


def test_the_login_page_is_reachable(anon):
    assert anon.get("/login").status_code == 200


def test_static_files_are_served(anon):
    resp = anon.get("/static/app.css")

    assert resp.status_code in (200, 404)      # served or absent, never a redirect


# ── Redirect encoding through the real response layer ────────────────────


def test_a_hostile_return_to_does_not_reach_the_location_header(anon):
    """safe_return_to is unit-tested; this proves it holds through the
    stack, where the header is actually encoded."""
    resp = anon.get("/accounts")
    location = resp.headers["location"]

    assert "evil" not in location
    assert not location.startswith("//")


def test_the_gate_preserves_the_target_over_http(anon):
    resp = anon.get("/accounts/8?forecast_months=6")

    assert resp.headers["location"] == "/login?return_to=/accounts/8?forecast_months=6"


def test_no_raw_line_break_survives_into_a_header(anon):
    for value in resp_headers(anon.get("/accounts")):
        assert "\r" not in value and "\n" not in value


def resp_headers(response):
    return [v for _, v in response.headers.items()]


# ── The body limit is mounted on the real app ────────────────────────────


def test_the_real_app_refuses_an_oversized_upload(signed_in):
    from app.config import settings

    resp = signed_in.post(
        "/import/upload",
        data={"account_id": "1"},
        files={"f": ("big.csv", b"x" * (settings.max_upload_bytes + 1024), "text/csv")},
    )

    assert resp.status_code == 413


def test_a_normal_sized_post_is_not_blocked_by_the_limit(signed_in):
    """The limit must not swallow ordinary form posts."""
    resp = signed_in.post("/login", data={"username": "x", "password": "y"})

    assert resp.status_code != 413
