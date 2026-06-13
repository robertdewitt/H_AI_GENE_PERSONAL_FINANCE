"""Minimal password-based login / logout.

WebAuthn / passkey ceremonies live under /auth/webauthn/* (Phase 2.3).
This module is the fallback path so a user with a session cookie that
expired can still sign back in without re-running /setup. It also acts
as the redirect target for ``get_current_user`` on HTML routes.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services.rate_limit import login_limiter
from app.services.sessions import create_session, revoke_session
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "auth/login.html", {
        "error": request.query_params.get("error"),
        "return_to": request.query_params.get("return_to", "/"),
    })


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    return_to: str = Form("/"),
    db: Session = Depends(get_db),
):
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHashError

    # Rate limit: 10 attempts per 15 minutes per (IP, username) — both
    # axes so one user's typo doesn't lock the whole network out.
    client_ip = (request.client.host if request.client else "unknown")
    rate_key = f"{client_ip}:{username.strip().lower()}"
    if not login_limiter.hit(rate_key):
        import urllib.parse as _u
        return RedirectResponse(
            url=f"/login?error={_u.quote('Too many attempts — try again later')}&return_to={_u.quote(return_to)}",
            status_code=303,
        )

    user = db.execute(
        select(User).where(User.username == username.strip()).limit(1)
    ).scalar_one_or_none()

    err = None
    if user is None or not user.password_hash:
        err = "Invalid credentials"
    else:
        try:
            PasswordHasher().verify(user.password_hash, password)
        except (VerifyMismatchError, InvalidHashError, ValueError):
            err = "Invalid credentials"

    if err:
        # Same message for unknown user and bad password — no enumeration leak.
        import urllib.parse as _u
        return RedirectResponse(
            url=f"/login?error={_u.quote(err)}&return_to={_u.quote(return_to)}",
            status_code=303,
        )

    token = create_session(db, user.id)
    # Don't redirect to arbitrary off-site URLs.
    safe_target = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/"
    response = RedirectResponse(url=safe_target, status_code=303)
    response.set_cookie(
        "session", token,
        httponly=True, samesite="lax", secure=False,
        max_age=60 * 60 * 24 * 7,
    )
    return response


@router.post("/logout")
def logout(
    request: Request,
    db: Session = Depends(get_db),
):
    raw = request.cookies.get("session")
    if raw:
        revoke_session(db, raw)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    return response
