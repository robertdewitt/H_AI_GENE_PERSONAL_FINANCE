"""WebAuthn ceremony endpoints.

Public endpoints:
* POST /auth/webauthn/login/options   — generate auth challenge
* POST /auth/webauthn/login/verify    — verify auth response, set session

Authenticated endpoints (logged-in user enrolling a new passkey):
* POST /auth/webauthn/register/options — generate registration challenge
* POST /auth/webauthn/register/verify  — verify response, persist credential

The browser-side JS (templates/auth/login.html, templates/settings/security.html)
invokes navigator.credentials.create() / .get() against these endpoints.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.models.webauthn_credential import WebAuthnCredential
from app.services.auth import get_current_user
from app.services.clock import naive_utc_now
from app.services.sessions import create_session
from app.services.webauthn_service import (
    begin_authentication, begin_registration,
    finish_authentication, finish_registration,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/webauthn", tags=["auth"])


# ── Registration (authenticated) ──────────────────────────────────────


def _request_origin(request: Request) -> str:
    """Reconstruct the origin the browser is actually using.

    WebAuthn refuses to accept a credential whose rp_id doesn't match the
    page's effective hostname. The request.url Starlette exposes carries
    the right scheme + host + port because of TrustedHost/ProxyHeaders.
    """
    return f"{request.url.scheme}://{request.url.netloc}"


@router.post("/register/options")
def webauthn_register_options(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.execute(
        select(WebAuthnCredential.credential_id)
        .where(WebAuthnCredential.user_id == user.id)
    ).scalars().all()
    return JSONResponse(begin_registration(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        existing_credential_ids=list(existing),
        origin=_request_origin(request),
    ))


@router.post("/register/verify")
def webauthn_register_verify(
    request: Request,
    body: dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify a navigator.credentials.create() response.

    The browser sends a single flat JSON: {label, id, rawId, type, response: {...}}.
    We split the credential payload from the label so py_webauthn sees the
    shape it expects.
    """
    label = body.pop("label", "Passkey") or "Passkey"
    try:
        verified = finish_registration(user.id, body, origin=_request_origin(request))
    except Exception as exc:
        log.warning("WebAuthn registration verify failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Registration verification failed: {exc}",
        )

    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=verified["credential_id"],
        public_key=verified["public_key"],
        sign_count=verified["sign_count"],
        label=label,
    )
    db.add(cred)
    db.commit()
    return {"ok": True, "credential_id_b64": base64.urlsafe_b64encode(
        verified["credential_id"]).decode("ascii").rstrip("=")}


# ── Authentication (unauthenticated — preflight to /login) ────────────


@router.post("/login/options")
def webauthn_login_options(
    request: Request,
    username: str = Form(...),
    db: Session = Depends(get_db),
):
    """Generate authentication options for a username.

    We deliberately mint an options block for *every* request so the
    response shape doesn't leak whether the username exists. When the
    username is unknown we hand the browser an empty allow_credentials
    list, which results in a failed ceremony — same outcome as a wrong
    user, no enumeration.
    """
    user = db.execute(
        select(User).where(User.username == username.strip()).limit(1)
    ).scalar_one_or_none()
    cred_ids: list[bytes] = []
    if user is not None:
        cred_ids = list(db.execute(
            select(WebAuthnCredential.credential_id)
            .where(WebAuthnCredential.user_id == user.id)
        ).scalars().all())
    return JSONResponse(begin_authentication(
        username_key=username.strip(),
        allow_credential_ids=cred_ids,
        origin=_request_origin(request),
    ))


@router.post("/login/verify")
def webauthn_login_verify(
    request: Request,
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Verify navigator.credentials.get() and issue a session cookie.

    Browser sends a single flat JSON with username + return_to + the WebAuthn
    credential payload. We split out the wrapping fields and hand the rest
    to py_webauthn.
    """
    username = (body.pop("username", "") or "").strip()
    return_to = body.pop("return_to", "/") or "/"
    payload = body

    if not username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Username required",
        )

    user = db.execute(
        select(User).where(User.username == username).limit(1)
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials",
        )

    # Decode the credential id from the browser response.
    raw_cid_b64 = payload.get("id", "")
    padding = "=" * (-len(raw_cid_b64) % 4)
    try:
        raw_cid = base64.urlsafe_b64decode(raw_cid_b64 + padding)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed credential id",
        )

    cred = db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.user_id == user.id,
            WebAuthnCredential.credential_id == raw_cid,
        ).limit(1)
    ).scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials",
        )

    try:
        new_count = finish_authentication(
            username_key=username,
            credential_json=payload,
            stored_public_key=cred.public_key,
            stored_sign_count=cred.sign_count,
            origin=_request_origin(request),
        )
    except Exception as exc:
        log.warning("WebAuthn login verify failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials",
        )

    cred.sign_count = new_count
    cred.last_used_at = naive_utc_now()
    db.commit()

    token = create_session(db, user.id)
    safe_target = (
        return_to if return_to.startswith("/") and not return_to.startswith("//")
        else "/"
    )
    response = JSONResponse({"ok": True, "redirect": safe_target})
    response.set_cookie(
        "session", token,
        httponly=True, samesite="lax", secure=False,
        max_age=60 * 60 * 24 * 7,
    )
    return response
