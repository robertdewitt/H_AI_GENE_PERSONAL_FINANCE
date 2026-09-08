"""The request body is bounded before anything parses it.

The upload route's own cap governs the copy into uploads/, which runs after
FastAPI has already parsed the multipart body — a 60 MB post against a 25 MB
cap was measured fully spooled to disk before the cap saw a byte. This
middleware refuses the body itself.

These are HTTP-level tests on purpose. The rest of the suite calls route
functions directly, which never exercises the ASGI layer where this lives.
"""
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.body_limit import BodySizeLimitMiddleware

LIMIT = 1024 * 1024      # 1 MB, to keep the tests quick


@pytest.fixture
def client():
    reached = {"body_bytes": None}

    async def sink(request):
        body = await request.body()
        reached["body_bytes"] = len(body)
        return JSONResponse({"bytes": len(body)})

    async def hello(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/sink", sink, methods=["POST"]),
        Route("/hello", hello, methods=["GET"]),
    ])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=LIMIT)
    c = TestClient(app)
    c.reached = reached
    return c


def test_a_body_under_the_limit_is_delivered(client):
    resp = client.post("/sink", content=b"x" * (LIMIT // 2))

    assert resp.status_code == 200
    assert resp.json()["bytes"] == LIMIT // 2


def test_exactly_at_the_limit_is_allowed(client):
    resp = client.post("/sink", content=b"x" * LIMIT)

    assert resp.status_code == 200


def test_an_oversized_body_is_refused_with_413(client):
    resp = client.post("/sink", content=b"x" * (LIMIT + 1))

    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"].lower()


def test_the_handler_never_sees_an_oversized_body(client):
    """The point of the middleware: the route is not reached at all, so
    nothing is spooled to disk on its behalf."""
    client.post("/sink", content=b"x" * (LIMIT * 4))

    assert client.reached["body_bytes"] is None


def test_an_oversized_multipart_upload_is_refused(client):
    """The real shape of the threat — a file part, not a raw body."""
    resp = client.post(
        "/sink", files={"file": ("big.csv", b"x" * (LIMIT * 3), "text/csv")},
    )

    assert resp.status_code == 413
    assert client.reached["body_bytes"] is None


def test_a_chunked_body_with_no_declared_length_is_still_counted(client):
    """Content-Length is the fast path; a streamed body has none, so the
    bytes have to be counted as they arrive."""
    def oversized_chunks():
        for _ in range(4):
            yield b"x" * (LIMIT // 2)

    resp = client.post("/sink", content=oversized_chunks())

    assert resp.status_code == 413
    assert client.reached["body_bytes"] is None


def test_a_chunked_body_under_the_limit_still_works(client):
    def small_chunks():
        yield b"a" * 1000
        yield b"b" * 1000

    resp = client.post("/sink", content=small_chunks())

    assert resp.status_code == 200
    assert resp.json()["bytes"] == 2000


def test_get_requests_are_untouched(client):
    assert client.get("/hello").status_code == 200


def test_a_lying_content_length_does_not_get_through(client):
    """A declared length under the limit with more bytes behind it must
    still be caught by the streaming count."""
    resp = client.post(
        "/sink",
        content=b"x" * (LIMIT * 2),
        headers={"content-length": str(LIMIT // 2)},
    )

    assert resp.status_code in (413, 400)
    assert client.reached["body_bytes"] != LIMIT * 2
