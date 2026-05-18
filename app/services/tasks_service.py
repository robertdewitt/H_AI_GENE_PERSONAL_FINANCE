"""Tasks / health-check service.

Produces a list of actionable items for the user — stale accounts,
unconfirmed transfers, uncategorized transactions, and duplicate groups.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

STALE_DAYS = 30   # flag accounts not updated within this many days


@dataclass
class Task:
    category: str        # "statements" | "transfers" | "categories" | "duplicates"
    title: str
    detail: str
    count: int
    url: str
    severity: str = "warning"   # "warning" | "info"


def get_tasks(db: Session) -> list[Task]:
    from app.models.account import Account
    from app.models.transaction import Transaction
    from app.models.transfer_link import TransferLink
    from app.models.dismissed_duplicate import DismissedDuplicate
    from app.services.duplicate_detector import find_duplicate_groups

    tasks: list[Task] = []
    now = datetime.now()
    cutoff = now - timedelta(days=STALE_DAYS)

    # ── 1. Stale accounts (no statement update in 30+ days) ──────────────────
    # Only flag transactional accounts (not real estate / vehicle / collectibles)
    from app.models.account import ASSET_TYPES, LIABILITY_TYPES, AccountType
    transactional_types = {
        AccountType.CHECKING, AccountType.SAVINGS,
        AccountType.CREDIT_CARD, AccountType.LOAN, AccountType.MORTGAGE,
        AccountType.BROKERAGE,
    }

    accounts = db.execute(select(Account)).scalars().all()
    stale_accounts: list[Account] = []
    for acct in accounts:
        if acct.account_type not in transactional_types:
            continue
        # Use most recent transaction date as proxy for "last updated"
        last_txn_date = db.execute(
            select(func.max(Transaction.date)).where(
                Transaction.account_id == acct.id
            )
        ).scalar()
        if last_txn_date is None or last_txn_date < cutoff:
            stale_accounts.append(acct)

    if stale_accounts:
        names = ", ".join(a.name for a in stale_accounts[:3])
        if len(stale_accounts) > 3:
            names += f" +{len(stale_accounts) - 3} more"
        tasks.append(Task(
            category="statements",
            title=f"{len(stale_accounts)} account{'s' if len(stale_accounts) != 1 else ''} not updated in {STALE_DAYS}+ days",
            detail=names,
            count=len(stale_accounts),
            url="/accounts",
            severity="warning",
        ))

    # ── 2. Unconfirmed transfers ─────────────────────────────────────────────
    unconfirmed = db.execute(
        select(func.count(TransferLink.id)).where(
            TransferLink.confirmed_by_user == False  # noqa: E712
        )
    ).scalar() or 0

    if unconfirmed:
        tasks.append(Task(
            category="transfers",
            title=f"{unconfirmed} unconfirmed transfer{'s' if unconfirmed != 1 else ''}",
            detail="Detected transfers awaiting your confirmation",
            count=unconfirmed,
            url="/transfers",
            severity="warning",
        ))

    # ── 3. Uncategorized transactions ────────────────────────────────────────
    uncategorized = db.execute(
        select(func.count(Transaction.id)).where(
            Transaction.category_id.is_(None),
            Transaction.is_transfer == False,  # noqa: E712
        )
    ).scalar() or 0

    if uncategorized:
        # Break down by account for the detail line
        rows = db.execute(
            select(Account.name, func.count(Transaction.id))
            .join(Transaction, Transaction.account_id == Account.id)
            .where(
                Transaction.category_id.is_(None),
                Transaction.is_transfer == False,  # noqa: E712
            )
            .group_by(Account.id)
            .order_by(func.count(Transaction.id).desc())
            .limit(3)
        ).all()
        detail_parts = [f"{name} ({cnt})" for name, cnt in rows]
        if len(rows) == 3:
            detail_parts.append("…")
        tasks.append(Task(
            category="categories",
            title=f"{uncategorized:,} transaction{'s' if uncategorized != 1 else ''} missing a category",
            detail=", ".join(detail_parts),
            count=uncategorized,
            url="/transactions/auto-categorize/preview",
            severity="warning" if uncategorized > 50 else "info",
        ))

    # ── 4. Duplicate groups ──────────────────────────────────────────────────
    dup_groups = find_duplicate_groups(db)
    cross_batch = [g for g in dup_groups if g.cross_batch]
    same_batch = [g for g in dup_groups if not g.cross_batch]

    if cross_batch:
        tasks.append(Task(
            category="duplicates",
            title=f"{len(cross_batch)} likely duplicate{'s' if len(cross_batch) != 1 else ''} (cross-file)",
            detail="Same transaction imported from two different files",
            count=len(cross_batch),
            url="/transactions/duplicates",
            severity="warning",
        ))

    if same_batch:
        tasks.append(Task(
            category="duplicates",
            title=f"{len(same_batch)} possible duplicate{'s' if len(same_batch) != 1 else ''} (same file)",
            detail="Same date & amount within one import batch",
            count=len(same_batch),
            url="/transactions/duplicates",
            severity="info",
        ))

    return tasks
