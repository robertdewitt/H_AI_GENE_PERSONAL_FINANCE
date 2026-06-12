"""First-run /setup claim route + redirect-everything-else middleware.

When the database has owned data but zero users, every HTML route 302s
to /setup until the owner registers and claims that data. The claim
itself is wrapped in a single transaction with a pre-write SQLite
backup and a post-write integrity check.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.user_profile import UserProfile
from app.services.setup_claim import (
    BackupFailed, ClaimIntegrityError,
    backup_sqlite_db, claim_all_rows, format_integrity_summary,
    has_existing_data, snapshot_row_counts, verify_claim_integrity,
)
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])


def _user_count(db: Session) -> int:
    from sqlalchemy import func as _func
    return int(db.execute(select(_func.count(User.id))).scalar() or 0)


def needs_setup(db: Session) -> bool:
    """True when there are no users yet (regardless of whether data exists).

    Used by the middleware in :mod:`app.main` to gate every other route.
    """
    return _user_count(db) == 0


@router.get("", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    if _user_count(db) > 0:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "setup/index.html", {
        "has_existing_data": has_existing_data(db),
        "error": request.query_params.get("error"),
    })


@router.post("")
def setup_claim(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Create the admin user, back up the DB, and claim every existing row.

    All writes happen in one transaction so a backup-or-integrity failure
    leaves the database in its pre-claim state.
    """
    if _user_count(db) > 0:
        return RedirectResponse(url="/", status_code=303)

    username = (username or "").strip()
    display_name = (display_name or "").strip() or username
    if not username or not password:
        return RedirectResponse(
            url="/setup?error=Username+and+password+are+required",
            status_code=303,
        )

    # Take a backup BEFORE any writes. If we can't write the backup, we
    # refuse to continue — the brief is explicit about this.
    try:
        backup_path = backup_sqlite_db(settings.database_url)
    except BackupFailed as exc:
        log.error("Pre-claim backup failed: %s", exc)
        return RedirectResponse(
            url=f"/setup?error=Could+not+write+backup%3A+{exc}",
            status_code=303,
        )

    # Capture pre-claim row counts so the integrity check can prove
    # nothing was dropped.
    pre_counts = snapshot_row_counts(db)

    # Hash the password with argon2 — the WebAuthn enrolment happens on
    # the next page so the user always has at least one auth method.
    from argon2 import PasswordHasher
    password_hash = PasswordHasher().hash(password)

    try:
        admin = User(
            username=username,
            display_name=display_name,
            password_hash=password_hash,
            is_admin=True,
        )
        db.add(admin)
        db.flush()

        claim_all_rows(db, admin.id)

        # Ensure the admin gets a UserProfile too (existing rows may have
        # been claimed, but a fresh-install path needs one created).
        existing_profile = db.execute(
            select(UserProfile).where(UserProfile.user_id == admin.id).limit(1)
        ).scalar_one_or_none()
        if existing_profile is None:
            db.add(UserProfile(user_id=admin.id, display_currency="USD"))

        summary = verify_claim_integrity(db, pre_counts, admin.id)
        log.info("\n%s", format_integrity_summary(summary))
        db.commit()
    except ClaimIntegrityError as exc:
        db.rollback()
        log.error(
            "Claim integrity check FAILED. Database rolled back. Backup: %s\n%s",
            backup_path, format_integrity_summary(exc.summary),
        )
        return RedirectResponse(
            url=f"/setup?error=Claim+integrity+check+failed.+Backup+at+{backup_path}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        log.exception("Unexpected setup-claim failure: %s", exc)
        return RedirectResponse(
            url=f"/setup?error=Setup+failed%3A+{exc}.+Backup+at+{backup_path}",
            status_code=303,
        )

    # Issue a session cookie so the user lands logged in.
    from app.services.sessions import create_session
    session_token = create_session(db, admin.id)
    response = RedirectResponse(url="/?welcome=1", status_code=303)
    response.set_cookie(
        "session", session_token,
        httponly=True,
        samesite="lax",
        secure=False,  # localhost — flipped to True in non-dev settings (Phase 2.3)
        max_age=60 * 60 * 24 * 7,  # idle window matches session expiry
    )
    return response
