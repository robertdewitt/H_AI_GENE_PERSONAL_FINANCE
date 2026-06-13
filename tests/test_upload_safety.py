"""Path-traversal regression tests for upload-handling code.

A client-supplied ``UploadFile.filename`` cannot be trusted. These tests
guarantee the sanitiser strips any directory components, NUL bytes,
control characters, and dot-only basenames, and that the destination
helper always lands strictly inside the configured upload directory.
"""
from pathlib import Path

import pytest

from app.services.upload_safety import (
    UnsafeFilenameError,
    assert_user_owns_path,
    safe_upload_dest,
    sanitize_filename,
    user_upload_dir,
)


def test_sanitize_strips_directory_components():
    assert sanitize_filename("../../evil.csv") == "evil.csv"
    assert sanitize_filename("/etc/passwd") == "passwd"
    assert sanitize_filename("a/b/c/file.pdf") == "file.pdf"


def test_sanitize_rejects_empty_or_dot_names():
    for bad in (None, "", ".", "..", "/", "//", "/./"):
        with pytest.raises(UnsafeFilenameError):
            sanitize_filename(bad)


def test_sanitize_strips_control_chars():
    assert sanitize_filename("ev\x00il.csv") == "evil.csv"
    assert sanitize_filename("a\x01b\x1fc.pdf") == "abc.pdf"


def test_dest_stays_inside_upload_dir(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    for malicious in ("../../evil.csv", "/etc/passwd", "..\\..\\boot.ini"):
        dest = safe_upload_dest(upload_dir, malicious)
        # Path must be strictly under uploads/ — relative_to raises otherwise.
        dest.relative_to(upload_dir.resolve())
        assert dest.parent == upload_dir.resolve()


def test_dest_prefixes_unique_token(tmp_path: Path):
    """Two uploads with the same client filename must not collide."""
    upload_dir = tmp_path / "uploads"
    a = safe_upload_dest(upload_dir, "statement.pdf")
    b = safe_upload_dest(upload_dir, "statement.pdf")
    assert a != b
    assert a.name.endswith("_statement.pdf")
    assert b.name.endswith("_statement.pdf")


def test_dest_creates_missing_upload_dir(tmp_path: Path):
    target = tmp_path / "does" / "not" / "exist"
    dest = safe_upload_dest(target, "x.csv")
    assert dest.parent.exists()
    assert dest.parent == target.resolve()


def test_dest_rejects_unsafe_names(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    with pytest.raises(UnsafeFilenameError):
        safe_upload_dest(upload_dir, "")
    with pytest.raises(UnsafeFilenameError):
        safe_upload_dest(upload_dir, "..")


# ── Per-user upload scoping ───────────────────────────────────────────


def test_user_scoped_dest_lands_in_subdir(tmp_path: Path):
    """safe_upload_dest with user_id places the file under
    uploads/<user_id>/ so two users' uploads can't collide."""
    upload_dir = tmp_path / "uploads"
    dest_a = safe_upload_dest(upload_dir, "statement.pdf", user_id=1)
    dest_b = safe_upload_dest(upload_dir, "statement.pdf", user_id=2)
    assert dest_a.parent == (upload_dir / "1").resolve()
    assert dest_b.parent == (upload_dir / "2").resolve()
    assert dest_a != dest_b


def test_user_upload_dir_is_idempotent(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    a1 = user_upload_dir(upload_dir, 7)
    a2 = user_upload_dir(upload_dir, 7)
    assert a1 == a2 and a1.is_dir()


def test_assert_user_owns_path_accepts_own_file(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    dest = safe_upload_dest(upload_dir, "mine.csv", user_id=5)
    dest.write_text("hi")
    # Same user — passes through.
    resolved = assert_user_owns_path(upload_dir, 5, dest)
    assert resolved == dest.resolve()


def test_assert_user_owns_path_rejects_other_users_file(tmp_path: Path):
    """The flaw this guards: an authenticated attacker passing a peer's
    fresh upload path to a confirm endpoint."""
    upload_dir = tmp_path / "uploads"
    alice_file = safe_upload_dest(upload_dir, "alice.csv", user_id=1)
    alice_file.write_text("alice")
    with pytest.raises(UnsafeFilenameError):
        assert_user_owns_path(upload_dir, 2, alice_file)


def test_assert_user_owns_path_rejects_traversal_outside_root(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_upload_dir(upload_dir, 1)
    # Anything outside uploads/ entirely is also rejected.
    elsewhere = tmp_path / "elsewhere.csv"
    elsewhere.write_text("x")
    with pytest.raises(UnsafeFilenameError):
        assert_user_owns_path(upload_dir, 1, elsewhere)


def test_assert_user_owns_path_requires_existing_file(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_root = user_upload_dir(upload_dir, 1)
    missing = user_root / "never_uploaded.csv"
    with pytest.raises(UnsafeFilenameError):
        assert_user_owns_path(upload_dir, 1, missing)
