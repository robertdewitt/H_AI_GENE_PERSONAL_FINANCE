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


def format_build_lines(semver: str) -> tuple[str, str]:
    """Return (footer_line, dashboard_short_line) with version, git, times."""
    sha = _git("rev-parse", "--short", "HEAD")
    dirty = _git_dirty()
    if sha:
        sha_disp = f"{sha}+dirty" if dirty else sha
    else:
        sha_disp = "no-git"

    committed_raw = _git("log", "-1", "--format=%cI")
    committed = _parse_git_iso(committed_raw)
    if committed:
        committed_fmt = committed.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        committed_fmt = "unknown"

    started = datetime.now(timezone.utc)
    started_fmt = started.strftime("%Y-%m-%d %H:%M:%S UTC")

    footer = (
        f"v{semver} · build {sha_disp} · committed {committed_fmt} "
        f"· server {started_fmt}"
    )
    dash_short = f"v{semver} · {sha_disp} · committed {committed_fmt}"
    return footer, dash_short
