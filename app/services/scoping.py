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
