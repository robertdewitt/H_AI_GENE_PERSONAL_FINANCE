"""Runtime build identity: semver from config + git SHA, commit time, server start.

Static parts (SHA, commit count, branch, commit date, start time) are computed
once at module import.  The dirty flag is re-evaluated on every call to
get_footer() / get_short() so it reflects the actual working-tree state without
requiring a server restart after a commit.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if proc.returncode != 0:
            return None
        out = (proc.stdout or "").strip()
        return out if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_dirty() -> bool:
    """Check for uncommitted changes — cheap enough to call per request."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if proc.returncode != 0:
            return False
        return bool((proc.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _parse_git_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


# ── Static parts — computed once at startup ──────────────────────────

def _git_commit_count() -> int | None:
    raw = _git("rev-list", "--count", "HEAD")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


_SHA: str = _git("rev-parse", "--short", "HEAD") or "no-git"
_BRANCH: str | None = _git("rev-parse", "--abbrev-ref", "HEAD")
_COMMIT_COUNT: int | None = _git_commit_count()
_STARTED_FMT: str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

_committed_raw = _git("log", "-1", "--format=%cI")
_committed_dt = _parse_git_iso(_committed_raw)
_COMMITTED_FMT: str = (
    _committed_dt.strftime("%Y-%m-%d %H:%M UTC") if _committed_dt else "unknown"
)


def format_build_lines(semver: str) -> tuple[str, str]:
    """Kept for backwards compatibility — calls get_footer/get_short."""
    return get_footer(semver), get_short(semver)


def get_footer(semver: str) -> str:
    """Full footer line — dirty flag is live (reflects current working tree)."""
    count_str = str(_COMMIT_COUNT) if _COMMIT_COUNT is not None else "?"
    dirty_str = "+dirty" if _git_dirty() else ""
    branch_str = f" [{_BRANCH}]" if _BRANCH and _BRANCH != "HEAD" else ""
    return (
        f"v{semver}.{count_str}{dirty_str} · {_SHA}{branch_str} · "
        f"committed {_COMMITTED_FMT} · started {_STARTED_FMT}"
    )


def get_short(semver: str) -> str:
    """Short dashboard line — dirty flag is live."""
    count_str = str(_COMMIT_COUNT) if _COMMIT_COUNT is not None else "?"
    dirty_str = "+dirty" if _git_dirty() else ""
    branch_str = f" [{_BRANCH}]" if _BRANCH and _BRANCH != "HEAD" else ""
    return f"v{semver}.{count_str}{dirty_str} · {_SHA}{branch_str} · committed {_COMMITTED_FMT}"
