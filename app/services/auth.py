"""Authentication entry points used by FastAPI dependencies.

Two acceptable credentials, in this order:

1. ``session=<token>`` cookie — set by /login or the /setup-claim flow.
2. ``Authorization: Bearer <token>`` header — created via /settings/security
   for non-interactive callers (LLM agents). The token is hashed at rest
   and never stored in plaintext.

Routes outside ``/setup`` and ``/login`` require an authenticated user.
HTML routes get a 303 redirect to /login on failure; API routes get a
401 response.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.api_token import ApiToken
from app.models.user import User
from app.services.clock import naive_utc_now
from app.services.sessions import lookup_session

log = logging.getLogger(__name__)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _api_user(db: Session, raw_token: str) -> User | None:
    """Return the user owning ``raw_token``, or None if missing/revoked."""
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    row = db.execute(
        select(ApiToken).where(
            ApiToken.token_hash == token_hash,
            ApiToken.revoked_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    row.last_used_at = naive_utc_now()
    db.commit()
    return db.get(User, row.user_id)


def get_current_user(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the request to a :class:`User` or raise 401 / redirect.

    HTML routes (anything not under /api/) receive a 303 redirect to
    /login on a missing/invalid credential. API routes get a 401 JSON.
    """
    # 1. Session cookie
    user: User | None = None
    if session:
        sess = lookup_session(db, session)
        if sess is not None:
            user = db.get(User, sess.user_id)

    # 2. Bearer token (also accepted on HTML routes so the /docs UI works)
    if user is None:
        raw = _bearer_token(authorization)
        if raw:
            user = _api_user(db, raw)

    if user is not None:
        return user

    is_api = request.url.path.startswith("/api/")
    if is_api:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    # HTML — bounce to /login with a return-to so we land back here.
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="Not authenticated",
        headers={"Location": f"/login?return_to={target}"},
    )


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Dependency for routes restricted to admin users (e.g. /register)."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required",
        )
    return user
