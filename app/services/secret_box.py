"""Symmetric-encryption helpers for at-rest secrets.

Uses Fernet (AES-128-CBC + HMAC-SHA256, the cryptography package's
authenticated symmetric primitive). The Fernet key is derived from
``SECRET_KEY`` via HKDF-SHA256 with a fixed salt so the key is stable
across restarts — but never appears in the database itself.

On first launch without a ``SECRET_KEY`` environment variable we
generate one and persist it to ``.env`` so the next run picks up the
same secret. Losing this file means every encrypted column becomes
unreadable, so it's worth backing up alongside the database.
"""
from __future__ import annotations

import logging
import os
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)


_HKDF_SALT = b"financial-hygiene/v1/at-rest"
_HKDF_INFO = b"fernet-key"
_PREFIX = "fernet:"  # tag so we can tell ciphertext from plaintext at rest


def _resolve_secret_key() -> str:
    """Return the configured SECRET_KEY, generating + persisting one on
    first launch if it doesn't exist. The generated value lands in
    ``.env`` next to the project root so subsequent runs are stable.
    """
    val = os.environ.get("SECRET_KEY")
    if val:
        return val

    # Persist a fresh secret to .env so reload picks it up.
    env_path = Path(__file__).resolve().parents[2] / ".env"
    generated = secrets.token_urlsafe(48)
    line = f"SECRET_KEY={generated}\n"
    if env_path.exists():
        with env_path.open("a") as fh:
            fh.write(line)
    else:
        env_path.write_text(line)
    log.warning(
        "SECRET_KEY was not set — generated one and wrote it to %s. "
        "Back this file up: losing it makes every encrypted column unreadable.",
        env_path,
    )
    os.environ["SECRET_KEY"] = generated
    return generated


def _fernet() -> Fernet:
    raw = _resolve_secret_key().encode("utf-8")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(raw)
    return Fernet(urlsafe_b64encode(derived))


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt ``plaintext`` and return a ``fernet:`` prefixed token.

    Empty / None passes through unchanged so a "clear this field" save
    doesn't accidentally encrypt the empty string.
    """
    if plaintext is None or plaintext == "":
        return plaintext
    if is_encrypted(plaintext):
        # Already ciphertext — don't double-wrap.
        return plaintext
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str | None) -> str | None:
    """Decrypt a ``fernet:`` token. Plaintext / empty / None pass through."""
    if not value or not is_encrypted(value):
        return value
    token = value[len(_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        log.warning("secret_box: ciphertext failed to decrypt — wrong SECRET_KEY?")
        return None


def is_encrypted(value: str | None) -> bool:
    return bool(value) and isinstance(value, str) and value.startswith(_PREFIX)


def mask(value: str | None, visible_tail: int = 4) -> str:
    """Return a masked representation of ``value`` for display.

    ``decrypt`` it first if needed so the masking reveals real-tail
    plaintext characters, not random-looking ciphertext.
    """
    plain = decrypt(value) if is_encrypted(value) else value
    if not plain:
        return ""
    if len(plain) <= visible_tail:
        return "•" * len(plain)
    return "•" * (max(4, len(plain) - visible_tail)) + plain[-visible_tail:]
