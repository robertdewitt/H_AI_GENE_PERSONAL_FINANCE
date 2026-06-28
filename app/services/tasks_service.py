"""Tasks / health-check service.

Produces a list of actionable items for the user — stale accounts,
unconfirmed transfers, uncategorized transactions, and duplicate groups.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from app.services.clock import naive_utc_now

from sqlalchemy import func, select
from sqlalchemy.orm import Session

STALE_DAYS = 30     # flag accounts not updated within this many days
DUE_SOON_DAYS = 30  # surface scheduled payments due within this window


@dataclass
class Task:
    category: str        # "statements" | "transfers" | "categories" | "duplicates"
    title: str
    detail: str
    count: int
    url: str
    severity: str = "warning"   # "warning" | "info"


def get_tasks(db: Session, user_id: int | None = None) -> list[Task]:
    from app.models.account import Account
    from app.models.transaction import Transaction
    from app.models.transfer_link import TransferLink
    from app.models.dismissed_duplicate import DismissedDuplicate
    from app.services.duplicate_detector import find_duplicate_groups

    tasks: list[Task] = []
    now = naive_utc_now()
    cutoff = now - timedelta(days=STALE_DAYS)

    # ── 1. Stale accounts (no statement update in 30+ days) ──────────────────
    # Only flag transactional accounts (not real estate / vehicle / collectibles)
    from app.models.account import ASSET_TYPES, LIABILITY_TYPES, AccountType
    transactional_types = {
        AccountType.CHECKING, AccountType.SAVINGS,
        AccountType.CREDIT_CARD, AccountType.LOAN, AccountType.MORTGAGE,
        AccountType.BROKERAGE,
    }

    _q = select(Account)
    if user_id is not None:
        _q = _q.where(Account.user_id == user_id)
    accounts = db.execute(_q).scalars().all()
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
        # Normalise to naive so aware datetimes (from any tz-aware import path)
        # don't TypeError against the naive cutoff.
        if last_txn_date is not None and last_txn_date.tzinfo is not None:
            last_txn_date = last_txn_date.replace(tzinfo=None)
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
    from app.services.duplicate_detector import find_near_duplicate_groups
    dup_groups = find_duplicate_groups(db)
    near_groups = find_near_duplicate_groups(db)
    cross_batch = [g for g in dup_groups if g.cross_batch] + near_groups
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

    # ── 5. Overdue + upcoming scheduled payments ─────────────────────────────
    try:
        from app.models.scheduled_payment import ScheduledPayment
        from datetime import date

        today = date.today()
        soon_cutoff = today + timedelta(days=DUE_SOON_DAYS)

        # 5a. Overdue — past their due date and not yet matched off.
        overdue = db.execute(
            select(ScheduledPayment).where(
                ScheduledPayment.active.is_(True),
                ScheduledPayment.next_due_date < today,
            )
        ).scalars().all()
        if user_id is not None:
            overdue = [p for p in overdue if p.account and p.account.user_id == user_id]
        if overdue:
            names = ", ".join(p.description for p in overdue[:3])
            if len(overdue) > 3:
                names += f" +{len(overdue) - 3} more"
            tasks.append(Task(
                category="scheduled",
                title=f"{len(overdue)} overdue scheduled payment{'s' if len(overdue) != 1 else ''}",
                detail=names,
                count=len(overdue),
                url="/scheduled",
                severity="warning",
            ))

        # 5b. Due soon — one card per account with a payment due in the next
        #     DUE_SOON_DAYS days, so "any account with payments due within the
        #     next month" shows up on the tasks page.
        upcoming = db.execute(
            select(ScheduledPayment).where(
                ScheduledPayment.active.is_(True),
                ScheduledPayment.next_due_date >= today,
                ScheduledPayment.next_due_date <= soon_cutoff,
            ).order_by(ScheduledPayment.next_due_date)
        ).scalars().all()

        by_account: dict[int, list] = {}
        for p in upcoming:
            if user_id is not None and not (p.account and p.account.user_id == user_id):
                continue
            by_account.setdefault(p.account_id, []).append(p)

        for acct_id, pmts in by_account.items():
            acct = pmts[0].account
            acct_name = acct.name if acct else f"Account {acct_id}"
            soonest = pmts[0].next_due_date
            days_away = (soonest - today).days
            # Total outflow due in the window (payments are negative for bills).
            outflow = sum(-float(p.amount) for p in pmts if (p.amount or 0) < 0)
            sym = getattr(acct, "currency_symbol", "") if acct else ""
            detail_bits = [
                f"{p.description} ({p.next_due_date.strftime('%d %b')})"
                for p in pmts[:3]
            ]
            if len(pmts) > 3:
                detail_bits.append(f"+{len(pmts) - 3} more")
            detail = ", ".join(detail_bits)
            if outflow > 0:
                detail = f"{sym}{outflow:,.2f} due — " + detail
            tasks.append(Task(
                category="scheduled",
                title=(
                    f"{acct_name}: {len(pmts)} payment"
                    f"{'s' if len(pmts) != 1 else ''} due "
                    + ("today" if days_away == 0
                       else f"in {days_away} day{'s' if days_away != 1 else ''}")
                ),
                detail=detail,
                count=len(pmts),
                url=f"/accounts/{acct_id}",
                severity="warning" if days_away <= 7 else "info",
            ))
    except Exception:
        pass  # table may not exist yet on old DBs

    return tasks
