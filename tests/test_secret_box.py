"""Round-trip + mask tests for the at-rest secret box.

Sets SECRET_KEY explicitly so the helper doesn't try to write a fresh
one into the repo's .env during the test run.
"""
import os

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only-do-not-deploy")

from app.services.secret_box import decrypt, encrypt, is_encrypted, mask  # noqa: E402


def test_encrypt_decrypt_round_trip():
    plain = "rentcast_live_abcdef1234567890"
    ct = encrypt(plain)
    assert ct is not None and ct.startswith("fernet:")
    assert is_encrypted(ct)
    assert decrypt(ct) == plain


def test_encrypt_idempotent_does_not_double_wrap():
    ct = encrypt("alpha-bravo")
    ct2 = encrypt(ct)
    assert ct2 == ct


def test_none_and_empty_pass_through():
    assert encrypt(None) is None
    assert encrypt("") == ""
    assert decrypt(None) is None
    assert decrypt("") == ""


def test_mask_shows_last_four_chars_only():
    masked = mask("rentcast_live_abcdefgwxyz")
    assert masked.endswith("wxyz")
    assert "rentcast" not in masked
    # Encrypted input is decrypted before masking so the tail reveals real chars.
    ct = encrypt("rentcast_live_abcdefgwxyz")
    assert mask(ct).endswith("wxyz")


def test_mask_handles_short_secrets():
    assert mask("abcd") == "••••"
    assert mask(None) == ""


def test_decrypt_with_wrong_key_returns_none(monkeypatch):
    """A corrupted SECRET_KEY produces unreadable ciphertext rather than
    raising — the caller (usually a template render) treats it as missing.
    """
    ct = encrypt("payload")
    # Re-derive a different Fernet by changing the env mid-test.
    monkeypatch.setenv("SECRET_KEY", "a-totally-different-secret-key-zzzzzz")
    # Reload the module so its cached Fernet instance is rebuilt.
    import importlib
    import app.services.secret_box as sb
    importlib.reload(sb)
    assert sb.decrypt(ct) is None
    # Restore for any later tests in this process
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-pytest-only-do-not-deploy")
    importlib.reload(sb)
