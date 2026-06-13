"""/settings/security — passkey management, API tokens, password change.

Logged-in users can enrol additional passkeys (laptop + phone), revoke
old ones, mint API tokens for agents, and change their password. We
refuse to leave a user with zero auth methods, so removing the last
passkey when no password is set is blocked.
"""
from __future__ import annotations

import hashlib
import logging
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.api_token import ApiToken
from app.models.user import User
from app.models.webauthn_credential import WebAuthnCredential
from app.services.auth import get_current_user
from app.services.clock import naive_utc_now
from app.services.sessions import revoke_session
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/security", tags=["security"])


@router.get("", response_class=HTMLResponse)
def security_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    passkeys = db.execute(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.user_id == user.id)
        .order_by(WebAuthnCredential.created_at.desc())
    ).scalars().all()
    tokens = db.execute(
        select(ApiToken)
        .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc())
    ).scalars().all()
    return templates.TemplateResponse(request, "settings/security.html", {
        "user": user,
        "passkeys": passkeys,
        "tokens": tokens,
        "new_token": request.query_params.get("new_token"),
        "flash": request.query_params.get("flash"),
        "error": request.query_params.get("error"),
    })


@router.post("/passkey/{credential_id}/delete")
def delete_passkey(
    credential_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cred = db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.id == credential_id,
            WebAuthnCredential.user_id == user.id,
        ).limit(1)
    ).scalar_one_or_none()
    if cred is None:
        raise HTTPException(status_code=404, detail="Passkey not found")

    other_count = db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.user_id == user.id,
            WebAuthnCredential.id != credential_id,
        ).limit(1)
    ).scalar_one_or_none()

    if other_count is None and not user.password_hash:
        return RedirectResponse(
            url="/settings/security?error=Cannot+remove+the+last+sign-in+method",
            status_code=303,
        )

    db.delete(cred)
    db.commit()
    return RedirectResponse(url="/settings/security?flash=Passkey+removed", status_code=303)


# ── API tokens ─────────────────────────────────────────────────────────


@router.post("/api-token/new")
def mint_api_token(
    label: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    label = label.strip() or "Untitled token"
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    db.add(ApiToken(user_id=user.id, label=label, token_hash=token_hash))
    db.commit()
    # Show the raw token to the user exactly once.
    import urllib.parse as _u
    return RedirectResponse(
        url=f"/settings/security?new_token={_u.quote(raw)}",
        status_code=303,
    )


@router.post("/api-token/{token_id}/revoke")
def revoke_api_token(
    token_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    token = db.execute(
        select(ApiToken).where(
            ApiToken.id == token_id, ApiToken.user_id == user.id,
        ).limit(1)
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Token not found")
    token.revoked_at = naive_utc_now()
    db.commit()
    return RedirectResponse(url="/settings/security?flash=Token+revoked", status_code=303)


# ── Password change ──────────────────────────────────────────────────


@router.post("/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHashError

    if len(new_password) < 10:
        return RedirectResponse(
            url="/settings/security?error=Password+must+be+at+least+10+characters",
            status_code=303,
        )

    ph = PasswordHasher()
    if user.password_hash:
        try:
            ph.verify(user.password_hash, current_password)
        except (VerifyMismatchError, InvalidHashError, ValueError):
            return RedirectResponse(
                url="/settings/security?error=Current+password+is+wrong",
                status_code=303,
            )

    user.password_hash = ph.hash(new_password)
    db.commit()

    # Revoke the current session so the new password takes effect — user
    # signs in fresh on the next page.
    raw = request.cookies.get("session")
    if raw:
        revoke_session(db, raw)
    response = RedirectResponse(url="/login?flash=Password+updated", status_code=303)
    response.delete_cookie("session")
    return response
