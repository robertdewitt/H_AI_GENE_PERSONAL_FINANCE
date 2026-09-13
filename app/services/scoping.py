"""Tenant-isolation query helpers.

Every route that reads from an owned table goes through one of these
helpers instead of constructing its own ``select(Account)``. This makes
the isolation guarantee structural rather than sprinkled — a new
endpoint that forgets to scope fails closed in the route-walking
isolation test.
"""
from __future__ import annotations

from typing import Iterable, Type, TypeVar

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.category import Category
from app.models.category_rule import CategoryRule
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.models.user import User

M = TypeVar("M")


def _owned(db: Session, model: Type[M], user: User):
    """Return a select() statement filtered to ``user``'s rows."""
    return select(model).where(model.user_id == user.id)


# ── Accounts ──────────────────────────────────────────────────────────


def owned_accounts(db: Session, user: User) -> list[Account]:
    return db.execute(_owned(db, Account, user)).scalars().all()


def owned_account_ids(db: Session, user: User) -> list[int]:
    return [a.id for a in owned_accounts(db, user)]


def get_owned_account_or_404(db: Session, user: User, account_id: int) -> Account:
    """Return the account if it belongs to ``user``, else raise 404.

    404 (not 403) is deliberate — leaking existence of another user's
    resource would be a side-channel.
    """
    acct = db.execute(
        _owned(db, Account, user).where(Account.id == account_id).limit(1)
    ).scalar_one_or_none()
    if acct is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )
    return acct


# ── Transactions (scoped via owning account) ──────────────────────────


def owned_transaction_query(user: User):
    """Return a select() for transactions in ``user``'s accounts.

    Transaction itself doesn't carry user_id (intentional — reachable via
    Account). All queries go through this join.
    """
    return (
        select(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .where(Account.user_id == user.id)
    )


def get_owned_transaction_or_404(
    db: Session, user: User, transaction_id: int,
) -> Transaction:
    txn = db.execute(
        owned_transaction_query(user).where(Transaction.id == transaction_id).limit(1)
    ).scalar_one_or_none()
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )
    return txn


# ── Categories / rules / import batches ───────────────────────────────


def owned_categories(db: Session, user: User) -> list[Category]:
    return db.execute(_owned(db, Category, user)).scalars().all()


def owned_category_rules(db: Session, user: User) -> list[CategoryRule]:
    return db.execute(_owned(db, CategoryRule, user)).scalars().all()


def owned_import_batches(db: Session, user: User) -> list[ImportBatch]:
    return db.execute(_owned(db, ImportBatch, user)).scalars().all()


# ── Rows owned through their account ──────────────────────────────────
#
# These tables either carry no user_id or cannot be trusted to: rows created
# after the first-run claim by code paths that never set it were found with
# user_id NULL on 23 of 31 scheduled payments. Ownership therefore flows from
# the account, which the claim guarantees and which every constructor now
# sets. The join is the same shape as owned_transaction_query.


def _owned_via_account(model: Type[M], user: User):
    return (
        select(model)
        .join(Account, model.account_id == Account.id)
        .where(Account.user_id == user.id)
    )


def _one_or_404(db: Session, stmt, what: str):
    row = db.execute(stmt.limit(1)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found",
        )
    return row


def owned_scheduled_payment_query(user: User):
    from app.models.scheduled_payment import ScheduledPayment

    return _owned_via_account(ScheduledPayment, user)


def get_owned_scheduled_payment_or_404(db: Session, user: User, payment_id: int):
    from app.models.scheduled_payment import ScheduledPayment

    return _one_or_404(
        db, owned_scheduled_payment_query(user).where(ScheduledPayment.id == payment_id),
        "Scheduled payment",
    )


def owned_scheduled_payment_ids(db: Session, user: User, ids: list[int]) -> list[int]:
    """The subset of ``ids`` this user actually owns — for bulk actions."""
    from app.models.scheduled_payment import ScheduledPayment

    if not ids:
        return []
    return [
        p.id for p in db.execute(
            owned_scheduled_payment_query(user).where(ScheduledPayment.id.in_(ids))
        ).scalars().all()
    ]


def owned_transaction_ids(db: Session, user: User, ids: list[int]) -> list[int]:
    """The subset of ``ids`` this user actually owns — for bulk actions.

    A bulk endpoint must never act on an id just because it was posted;
    filtering the list here means a foreign id is silently dropped rather
    than acted on or reported.
    """
    if not ids:
        return []
    return [
        t.id for t in db.execute(
            owned_transaction_query(user).where(Transaction.id.in_(ids))
        ).scalars().all()
    ]


def get_owned_proposal_or_404(db: Session, user: User, proposal_id: int):
    from app.models.scheduled_match_proposal import ScheduledMatchProposal
    from app.models.scheduled_payment import ScheduledPayment

    stmt = (
        select(ScheduledMatchProposal)
        .join(ScheduledPayment,
              ScheduledMatchProposal.scheduled_payment_id == ScheduledPayment.id)
        .join(Account, ScheduledPayment.account_id == Account.id)
        .where(Account.user_id == user.id, ScheduledMatchProposal.id == proposal_id)
    )
    return _one_or_404(db, stmt, "Proposal")


def owned_deleted_transaction_query(user: User):
    from app.models.deleted_transaction import DeletedTransaction

    return _owned_via_account(DeletedTransaction, user)


def owned_dismissed_duplicate_query(user: User):
    from app.models.dismissed_duplicate import DismissedDuplicate

    return _owned_via_account(DismissedDuplicate, user)


def owner_of_account(db: Session, account_id: int | None) -> int | None:
    """The user_id an account belongs to, for stamping rows created under it.

    Every constructor of an account-scoped row uses this so ownership is
    written at creation rather than repaired later. Rows created after the
    first-run claim by paths that skipped it were found with user_id NULL —
    23 of 31 scheduled payments — which scoped queries would then hide from
    their own owner.
    """
    if account_id is None:
        return None
    acct = db.get(Account, account_id)
    return acct.user_id if acct is not None else None


def get_owned_transaction(db: Session, user: User, transaction_id: int) -> Transaction | None:
    """Non-raising twin of get_owned_transaction_or_404.

    The HTML routers already answer a missing row with their own 404 page or
    a redirect; this lets them keep that shape while a foreign id simply
    reads as absent. Existence of another user's row is never revealed.
    """
    return db.execute(
        owned_transaction_query(user).where(Transaction.id == transaction_id).limit(1)
    ).scalar_one_or_none()


def get_owned_scheduled_payment(db: Session, user: User, payment_id: int):
    """Non-raising twin of get_owned_scheduled_payment_or_404."""
    from app.models.scheduled_payment import ScheduledPayment

    return db.execute(
        owned_scheduled_payment_query(user).where(ScheduledPayment.id == payment_id).limit(1)
    ).scalar_one_or_none()


def get_owned_dismissed_scheduled_or_404(db: Session, user: User, dismissal_id: int):
    from app.models.dismissed_scheduled_payment import DismissedScheduledPayment

    return _one_or_404(
        db,
        _owned_via_account(DismissedScheduledPayment, user).where(
            DismissedScheduledPayment.id == dismissal_id
        ),
        "Dismissed payment",
    )
