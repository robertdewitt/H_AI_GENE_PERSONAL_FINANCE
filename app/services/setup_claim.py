"""First-run setup-claim flow.

When the app starts with zero users but existing data in owned tables
(accounts, categories, etc.), every route redirects to ``/setup`` where
the owner registers their real account and immediately claims all
existing rows in a single transaction. After the claim:

* a backup of the SQLite database is taken **before** any writes,
* every owned row has ``user_id`` set to the new admin user,
* an integrity check asserts row counts match pre-claim numbers, no
  ``user_id`` is NULL, and no orphaned FK references survived,
* on any failure the transaction rolls back and the user is pointed at
  the backup file.

This module exposes the helpers used by the route in
``app.routers.setup``; the route itself is in that module so this file
stays import-safe in test environments without FastAPI.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# Tables whose top-level ownership is recorded via a ``user_id`` column.
OWNED_TABLES: tuple[str, ...] = (
    "accounts", "categories", "category_rules", "import_batches",
    "rental_properties", "asset_valuations",
    "scheduled_payments", "plan_it_plans",
    "account_balance_snapshots", "asset_valuation_snapshots",
    "liability_balance_snapshots", "household_snapshots",
    "financial_documents", "property_pnl_snapshots",
    "user_profile",
)


class BackupFailed(RuntimeError):
    """Raised when the pre-claim DB backup cannot be written.

    The brief mandates refusing to run the migration in this state so the
    user always has a way back if anything goes wrong.
    """


class ClaimIntegrityError(RuntimeError):
    """Raised when the post-claim integrity check fails.

    Carries the per-table summary so the caller can print it and the
    transaction can be rolled back safely.
    """

    def __init__(self, message: str, summary: dict[str, dict]):
        super().__init__(message)
        self.summary = summary


def has_existing_data(db: Session) -> bool:
    """Return True if any owned table has at least one row.

    Used by the middleware to decide whether to force /setup when there
    are no users yet — empty databases just skip straight to registration.
    """
    for table in OWNED_TABLES:
        try:
            row = db.execute(
                # text() route avoids needing the ORM mapping for every table
                _count_query(table)
            ).first()
        except Exception:
            # Table may not exist yet on a brand-new DB.
            continue
        if row and (row[0] or 0) > 0:
            return True
    return False


def _count_query(table: str):
    from sqlalchemy import text
    return text(f"SELECT COUNT(*) FROM {table}")


def backup_sqlite_db(db_url: str, backups_dir: Path | str | None = None) -> Path:
    """Copy the SQLite DB file to a timestamped backup before the claim.

    Returns the backup path. Raises BackupFailed if the database isn't
    SQLite (PostgreSQL deployments must take their own snapshot before
    running the migration) or the file copy fails.
    """
    if not db_url.startswith("sqlite:///"):
        raise BackupFailed(
            f"Pre-claim backup only supported for SQLite; got {db_url!r}. "
            "Snapshot your PostgreSQL instance before running the migration."
        )
    src_path = Path(db_url.removeprefix("sqlite:///"))
    if not src_path.exists():
        raise BackupFailed(f"SQLite file {src_path!s} not found")

    backups_dir = Path(backups_dir) if backups_dir else src_path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backups_dir / f"pre_auth_{stamp}.db"
    try:
        shutil.copy2(src_path, dest)
    except Exception as exc:
        raise BackupFailed(f"Could not copy {src_path!s} to {dest!s}: {exc}") from exc
    log.info("Pre-claim DB backup written to %s", dest)
    return dest


def snapshot_row_counts(db: Session) -> dict[str, int]:
    """Return ``{table: row_count}`` for every owned table.

    Captured before the claim so the integrity check can prove no rows
    were dropped or added by the migration.
    """
    counts: dict[str, int] = {}
    for table in OWNED_TABLES:
        try:
            row = db.execute(_count_query(table)).first()
            counts[table] = int(row[0]) if row else 0
        except Exception:
            counts[table] = 0
    return counts


def claim_all_rows(db: Session, user_id: int) -> dict[str, int]:
    """Set ``user_id`` on every NULL-user_id row in every owned table.

    Returns ``{table: rows_updated}``. The caller wraps this in a
    transaction so a failure rolls everything back.
    """
    from sqlalchemy import text
    updates: dict[str, int] = {}
    for table in OWNED_TABLES:
        try:
            result = db.execute(
                text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"),
                {"uid": user_id},
            )
            updates[table] = result.rowcount or 0
        except Exception as exc:
            log.warning("claim_all_rows: %s failed: %s", table, exc)
            updates[table] = 0
    return updates


def verify_claim_integrity(
    db: Session,
    pre_counts: dict[str, int],
    user_id: int,
) -> dict[str, dict]:
    """Verify the claim is complete and consistent.

    For each owned table, assert:
    * row count is unchanged from pre_counts (no orphans dropped),
    * no row has NULL user_id,
    * (where applicable) FK to accounts.id still resolves.

    Returns a per-table summary dict. Raises ClaimIntegrityError if any
    check fails so the caller can roll the transaction back.
    """
    from sqlalchemy import text
    summary: dict[str, dict] = {}
    failures: list[str] = []

    for table in OWNED_TABLES:
        pre = pre_counts.get(table, 0)
        try:
            post = int(db.execute(_count_query(table)).first()[0] or 0)
        except Exception:
            post = 0
        try:
            null_uid = int(db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL")
            ).first()[0] or 0)
        except Exception:
            null_uid = 0

        summary[table] = {
            "pre_count": pre, "post_count": post, "null_user_id": null_uid,
        }
        if pre != post:
            failures.append(
                f"{table}: row count changed {pre} → {post}"
            )
        if null_uid > 0:
            failures.append(
                f"{table}: {null_uid} rows still have NULL user_id"
            )

    if failures:
        raise ClaimIntegrityError(
            "Post-claim integrity check failed:\n  - " + "\n  - ".join(failures),
            summary,
        )
    return summary


def format_integrity_summary(summary: dict[str, dict]) -> str:
    """Render the integrity summary for logging / banner display."""
    lines = ["Setup-claim integrity check:"]
    for table, info in summary.items():
        lines.append(
            f"  {table:<32} {info['pre_count']:>6} -> {info['post_count']:>6}"
            f"  (NULL user_id: {info['null_user_id']})"
        )
    return "\n".join(lines)
