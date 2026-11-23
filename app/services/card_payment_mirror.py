"""Put a card payment seen only at the bank onto the card it paid.

A card's statements don't always cover the month a payment landed — a new
Citi card came in with seven statements, none of them the December that
showed the $99 fee being paid — while the paying account's export shows
the outflow plainly ("Citi Credit Card Payment"). Left alone, that outflow
reads as spending and the card never shows the payment.

For each unlinked outflow that names a card's issuer and says it is a
payment:

* the card already has the inbound payment (same amount, within a week) —
  the two are linked as a transfer;
* it doesn't — the payment is added to the card, unbatched and without a
  running balance, and linked. A later statement that carries the real row
  merges into it through the importer's placeholder matching rather than
  duplicating it.

An issuer with more than one open card (two Amex, two BofA) only links to
a payment one of its cards already has: a bare "Amex payment" doesn't say
which card it was for.

``mirror_card_payments`` is idempotent: linked outflows are skipped.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.account import Account, AccountType
from app.models.enums import EconomicEventType
from app.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

WINDOW = timedelta(days=7)
SOURCE = "card_payment_mirror"
_PAYMENT_CUE = re.compile(r"credit\s*card|payment|\bpmt\b|autopay|e-?pay", re.I)

# What a bank export calls each issuer, beyond the institution name itself.
_ALIASES = {
    "american express": ("american express", "amex"),
    "bank of america": ("bank of america", "bofa", "boa "),
    "citi": ("citi",),
    "citibank": ("citi",),
}


def _transfer_category_id(db: "Session", user_id: int | None) -> int | None:
    from app.models.category import Category
    q = select(Category.id).where(Category.name == "Account Transfer")
    if user_id is not None:
        q = q.where((Category.user_id == user_id) | Category.user_id.is_(None))
    return db.execute(q.limit(1)).scalar()


def _mark_transfer(db: "Session", txn: Transaction, category_id: int | None) -> None:
    """Classify one side the way linked card payments already are: an
    Account Transfer, not spending."""
    from app.services.transaction_truth import apply_truth_after_transaction_update
    old = txn.event_type
    txn.event_type = EconomicEventType.INTERNAL_TRANSFER.value
    if category_id is not None:
        txn.category_id = category_id
    apply_truth_after_transaction_update(db, txn, old)


@dataclass
class MirrorResult:
    linked: list[tuple[int, int]] = field(default_factory=list)     # (bank txn, card txn) already there
    created: list[tuple[int, int]] = field(default_factory=list)    # (bank txn, new card txn)
    ambiguous: list[int] = field(default_factory=list)              # bank txns naming >1 card


def _issuer_keys(card: Account) -> tuple[str, ...]:
    inst = (card.institution or card.name.split("/")[0].split()[0]).strip().lower()
    return _ALIASES.get(inst, (inst,)) if inst else ()


def _names_card(desc: str, keys: tuple[str, ...]) -> bool:
    d = f" {desc.lower()} "
    return any(re.search(rf"(?<![a-z]){re.escape(k.strip())}(?![a-z])", d) for k in keys)


def mirror_card_payments(db: "Session", user_id: int | None = None, *, dry_run: bool = False) -> MirrorResult:
    from app.services.transfer_detector import link_transfer

    q = select(Account).where(Account.closed_at.is_(None))
    if user_id is not None:
        q = q.where(Account.user_id == user_id)
    accounts = db.execute(q).scalars().all()
    cards = [a for a in accounts if a.account_type == AccountType.CREDIT_CARD]
    payers = [a for a in accounts if a.is_asset]
    if not cards or not payers:
        return MirrorResult()

    result = MirrorResult()
    keys = {c.id: _issuer_keys(c) for c in cards}
    transfer_cat = _transfer_category_id(db, user_id)
    outflows = db.execute(
        select(Transaction).where(
            Transaction.account_id.in_([p.id for p in payers]),
            Transaction.amount < 0,
            Transaction.transfer_link_id.is_(None),
        )
    ).scalars().all()

    for out in outflows:
        desc = out.description or ""
        if not _PAYMENT_CUE.search(desc):
            continue
        payer = db.get(Account, out.account_id)
        named = [c for c in cards if keys[c.id] and _names_card(desc, keys[c.id])
                 and (c.currency or "").upper() == (payer.currency or "").upper()]
        if not named:
            continue
        amount = -Decimal(out.amount)

        # The card's own inbound payment, when it has one.
        existing = []
        for c in named:
            existing += db.execute(
                select(Transaction).where(
                    Transaction.account_id == c.id,
                    Transaction.amount == amount,
                    Transaction.transfer_link_id.is_(None),
                    Transaction.date.between(out.date - WINDOW, out.date + WINDOW),
                )
            ).scalars().all()
        if len(existing) == 1:
            if not dry_run:
                _mark_transfer(db, out, transfer_cat)
                _mark_transfer(db, existing[0], transfer_cat)
                link_transfer(db, out.id, existing[0].id, confidence=0.95, confirmed=True)
            result.linked.append((out.id, existing[0].id))
            continue
        if len(named) != 1 or existing:
            result.ambiguous.append(out.id)
            continue

        card = named[0]
        if dry_run:
            result.created.append((out.id, 0))
            continue
        mirror = Transaction(
            account_id=card.id,
            date=out.date,
            description=f"Payment from {payer.name}",
            amount=amount,
            original_currency=card.currency or out.original_currency,
            is_transfer=True,
            event_type=EconomicEventType.INTERNAL_TRANSFER.value,
            category_id=transfer_cat,
            raw_data=json.dumps({"source": SOURCE, "from_transaction_id": out.id}),
        )
        db.add(mirror)
        db.flush()
        _mark_transfer(db, out, transfer_cat)
        link_transfer(db, out.id, mirror.id, confidence=0.95, confirmed=True)
        result.created.append((out.id, mirror.id))

    if not dry_run and (result.linked or result.created):
        db.commit()
    return result
