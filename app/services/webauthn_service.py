"""WebAuthn registration + authentication ceremonies.

Wraps the ``py_webauthn`` package. Pending challenges are tracked in a
small in-memory dict with TTL — fine for a single-process server. A
restart simply invalidates any in-flight registration; the user just
clicks "Add passkey" again.

For a multi-worker production deployment, swap ``_PENDING`` for a
short-lived Redis or DB store keyed by username + ceremony.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import timedelta

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from app.config import settings
from app.services.clock import naive_utc_now

log = logging.getLogger(__name__)


CHALLENGE_TTL = timedelta(minutes=5)


@dataclass
class _Pending:
    challenge: bytes
    expires_at: object  # datetime, kept untyped to avoid the import dance


_PENDING: dict[str, _Pending] = {}
_PENDING_LOCK = threading.Lock()


def _stash(key: str, challenge: bytes) -> None:
    expires = naive_utc_now() + CHALLENGE_TTL
    with _PENDING_LOCK:
        _PENDING[key] = _Pending(challenge=challenge, expires_at=expires)
        # Opportunistic cleanup so the dict doesn't grow forever.
        now = naive_utc_now()
        for k in [k for k, v in _PENDING.items() if v.expires_at < now]:
            del _PENDING[k]


def _pop(key: str) -> bytes | None:
    with _PENDING_LOCK:
        entry = _PENDING.pop(key, None)
    if entry is None or entry.expires_at < naive_utc_now():
        return None
    return entry.challenge


def _b64url_decode(value: str) -> bytes:
    """Decode a base64url string, padding it back up to a multiple of 4."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


# ── Registration ──────────────────────────────────────────────────────


def begin_registration(
    user_id: int,
    username: str,
    display_name: str,
    existing_credential_ids: list[bytes],
) -> dict:
    """Generate registration options for navigator.credentials.create()."""
    opts = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=settings.rp_name,
        user_id=str(user_id).encode("utf-8"),
        user_name=username,
        user_display_name=display_name,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=cid) for cid in existing_credential_ids
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )
    _stash(f"reg:{user_id}", opts.challenge)
    # Returned as a JSON-safe dict for the browser.
    from webauthn.helpers import options_to_json
    return json.loads(options_to_json(opts))


def finish_registration(
    user_id: int,
    credential_json: dict,
) -> dict:
    """Verify a navigator.credentials.create() response and return the
    fields needed to persist a WebAuthnCredential row.
    """
    challenge = _pop(f"reg:{user_id}")
    if challenge is None:
        raise ValueError("No pending registration challenge for this user")

    verification = verify_registration_response(
        credential=credential_json,
        expected_challenge=challenge,
        expected_origin=settings.rp_origin,
        expected_rp_id=settings.rp_id,
    )
    return {
        "credential_id": verification.credential_id,
        "public_key": verification.credential_public_key,
        "sign_count": verification.sign_count or 0,
    }


# ── Authentication ────────────────────────────────────────────────────


def begin_authentication(
    username_key: str,
    allow_credential_ids: list[bytes],
) -> dict:
    """Generate authentication options keyed by ``username_key``.

    The key is whatever uniquely identifies the requester at this stage
    — typically the username typed at /login. We don't want it to leak
    which usernames exist, so the caller is expected to mint the same
    options regardless of whether the user is known.
    """
    opts = generate_authentication_options(
        rp_id=settings.rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=cid) for cid in allow_credential_ids
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _stash(f"auth:{username_key}", opts.challenge)
    from webauthn.helpers import options_to_json
    return json.loads(options_to_json(opts))


def finish_authentication(
    username_key: str,
    credential_json: dict,
    stored_public_key: bytes,
    stored_sign_count: int,
) -> int:
    """Verify a navigator.credentials.get() response.

    Returns the new sign_count to persist on the WebAuthnCredential row.
    Raises ValueError if the response can't be verified or the sign_count
    decreased (clone detection).
    """
    challenge = _pop(f"auth:{username_key}")
    if challenge is None:
        raise ValueError("No pending authentication challenge for this user")

    verification = verify_authentication_response(
        credential=credential_json,
        expected_challenge=challenge,
        expected_origin=settings.rp_origin,
        expected_rp_id=settings.rp_id,
        credential_public_key=stored_public_key,
        credential_current_sign_count=stored_sign_count,
    )
    return verification.new_sign_count
