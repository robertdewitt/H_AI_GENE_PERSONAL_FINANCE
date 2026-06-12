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
    safe_upload_dest,
    sanitize_filename,
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
