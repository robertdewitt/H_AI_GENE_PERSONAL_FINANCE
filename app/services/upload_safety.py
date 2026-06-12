"""Path-traversal-safe filename + destination helpers for upload routes.

A client-supplied filename can contain ``..``, absolute paths, NUL bytes,
or simply collide with an existing file. ``safe_upload_dest`` strips the
filename down to its basename, rejects empty/dot names, normalises
suspicious characters, prefixes with a short timestamp so concurrent
uploads don't overwrite each other, and asserts the final path stays
strictly inside ``upload_dir``.

Use it at every place ``UploadFile.filename`` touches the filesystem.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


class UnsafeFilenameError(ValueError):
    """Raised when a client filename cannot be safely stored under upload_dir."""


_FORBIDDEN_BASENAMES = {"", ".", ".."}
_INVALID_CHARS = re.compile(r"[\x00-\x1f\x7f]")  # control chars including NUL


def sanitize_filename(raw: str | None) -> str:
    """Return a safe basename derived from a client-supplied filename.

    Rules:
    * Strip directory components (``Path(raw).name`` only).
    * Reject empty / "." / ".." after stripping.
    * Strip control characters.
    * Replace os-separator chars left over (``/`` ``\\``) defensively.
    """
    if raw is None:
        raise UnsafeFilenameError("filename is empty")
    base = Path(raw).name  # discards directories on any OS
    base = _INVALID_CHARS.sub("", base)
    base = base.replace("/", "_").replace("\\", "_").strip()
    if base in _FORBIDDEN_BASENAMES:
        raise UnsafeFilenameError(f"unsafe filename: {raw!r}")
    return base


def safe_upload_dest(upload_dir: str | Path, client_filename: str | None) -> Path:
    """Produce a unique destination path strictly inside ``upload_dir``.

    Prefixes the sanitised basename with a UTC timestamp + short UUID so
    concurrent uploads with identical names don't overwrite each other,
    and so a malicious filename can't replace an unrelated file via a
    race. Verifies the final resolved path is inside ``upload_dir``.
    """
    base = sanitize_filename(client_filename)
    dir_path = Path(upload_dir).resolve()
    dir_path.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:8]
    dest = (dir_path / f"{stamp}_{short}_{base}").resolve()

    # Final containment check — even an oddly-encoded basename can't escape
    # because Path.resolve normalises ``..`` and symlinks.
    try:
        dest.relative_to(dir_path)
    except ValueError as exc:
        raise UnsafeFilenameError(
            f"resolved upload path {dest!s} escaped {dir_path!s}"
        ) from exc

    return dest
