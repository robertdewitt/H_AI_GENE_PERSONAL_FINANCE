"""Server-side session helpers.

Sessions are 256-bit random tokens stored as their SHA-256 hash in the
``sessions`` table. The cookie carries the raw token; verification is a
constant-time compare against the hash.

Idle expiry: 7 days since last_seen_at. Absolute expiry: 30 days since
created_at.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.session import Session as AuthSession
from app.services.clock import naive_utc_now

IDLE_DAYS = 7
ABSOLUTE_DAYS = 30


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(db: Session, user_id: int) -> str:
    """Create a session row and return the raw token (to set in a cookie)."""
    token = secrets.token_urlsafe(32)
    now = naive_utc_now()
    db.add(AuthSession(
        user_id=user_id,
        token_hash=_hash_token(token),
        created_at=now,
        expires_at=now + timedelta(days=ABSOLUTE_DAYS),
        last_seen_at=now,
    ))
    db.commit()
    return token


def lookup_session(db: Session, raw_token: str) -> AuthSession | None:
    """Return the live session matching ``raw_token``, or None if missing /
    expired / past its absolute deadline.

    Touches ``last_seen_at`` on a successful hit so idle expiry tracks
    real activity.
    """
    if not raw_token:
        return None
    row = db.execute(
        select(AuthSession).where(
            AuthSession.token_hash == _hash_token(raw_token)
        ).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    now = naive_utc_now()
    if row.expires_at < now:
        db.delete(row)
        db.commit()
        return None
    if (now - row.last_seen_at).days > IDLE_DAYS:
        db.delete(row)
        db.commit()
        return None
    row.last_seen_at = now
    db.commit()
    return row


def revoke_session(db: Session, raw_token: str) -> None:
    """Logout: delete the session row matching ``raw_token``."""
    if not raw_token:
        return
    db.execute(
        AuthSession.__table__.delete().where(
            AuthSession.token_hash == _hash_token(raw_token)
        )
    )
    db.commit()
