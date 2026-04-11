"""Runtime build identity: semver from config + git SHA, commit time, server start."""

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


def _git_commit_count() -> int | None:
    raw = _git("rev-list", "--count", "HEAD")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _git_branch() -> str | None:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


def format_build_lines(semver: str) -> tuple[str, str]:
    """Return (footer_line, dashboard_short_line) with version, git, times.

    The displayed version is  v{semver}.{commit_count}  so it increments
    automatically on every commit without manual version bumping.
    +dirty is appended when there are uncommitted changes.
    """
    sha = _git("rev-parse", "--short", "HEAD")
    dirty = _git_dirty()
    commit_count = _git_commit_count()
    branch = _git_branch()

    # Build the version string: semver + auto-incrementing commit count
    count_str = str(commit_count) if commit_count is not None else "?"
    version = f"v{semver}.{count_str}"
    if dirty:
        version += "+dirty"

    sha_disp = sha or "no-git"
    branch_disp = f" [{branch}]" if branch and branch != "HEAD" else ""

    committed_raw = _git("log", "-1", "--format=%cI")
    committed = _parse_git_iso(committed_raw)
    committed_fmt = committed.strftime("%Y-%m-%d %H:%M UTC") if committed else "unknown"

    started = datetime.now(timezone.utc)
    started_fmt = started.strftime("%Y-%m-%d %H:%M UTC")

    footer = (
        f"{version} · {sha_disp}{branch_disp} · "
        f"committed {committed_fmt} · started {started_fmt}"
    )
    dash_short = f"{version} · {sha_disp}{branch_disp} · committed {committed_fmt}"
    return footer, dash_short
