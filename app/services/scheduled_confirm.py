"""Turn a projected scheduled payment occurrence into a real ledger row.

A scheduled payment is only a projection: the forecast walks it forward one
period at a time and shows what the balance *would* be.  Confirming one
occurrence posts it to the account ledger as an ordinary Transaction — it
counts towards the balance exactly like an imported row — and advances the
schedule past that date so the forecast stops projecting it a second time.

The link back to the schedule lives in ``Transaction.raw_data`` so a repeated
submit (double-click, browser back-then-forward) is a no-op rather than a
duplicate ledger entry.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.services.clock import naive_utc_now

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.scheduled_payment import ScheduledPayment
    from app.models.transaction import Transaction

CONFIRM_SOURCE = "scheduled_confirm"


def find_confirmed(
    db: "Session", payment: "ScheduledPayment", occurrence_date: date,
) -> "Transaction | None":
    """The ledger row already posted for this payment on this date, if any."""
    from sqlalchemy import select

    from app.models.transaction import Transaction

    day_start = datetime.combine(occurrence_date, datetime.min.time())
    rows = db.execute(
        select(Transaction).where(
            Transaction.account_id == payment.account_id,
            Transaction.date >= day_start,
            Transaction.date < day_start + timedelta(days=1),
        )
    ).scalars().all()

    for txn in rows:
        if not txn.raw_data:
            continue
        try:
            meta = json.loads(txn.raw_data)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(meta, dict)
            and meta.get("source") == CONFIRM_SOURCE
            and meta.get("scheduled_payment_id") == payment.id
        ):
            return txn
    return None


def confirm_occurrence(
    db: "Session",
    payment: "ScheduledPayment",
    occurrence_date: date,
    amount: Decimal | None = None,
) -> "tuple[Transaction | None, bool]":
    """Post one projected occurrence to the ledger.

    ``amount`` is the amount the forecast displayed — for a ``variable``
    payment that is a trailing average rather than the stored anchor amount,
    so the ledger row matches what the user was looking at when they hit
    Confirm.  Falls back to the payment's own amount.

    Returns ``(transaction, created)``; ``created`` is False when this
    occurrence was already confirmed.
    """
    from app.models.account import Account
    from app.models.enums import ClassificationProvenance
    from app.models.transaction import Transaction
    from app.services.event_classifier import classify_transaction
    from app.services.scheduled_matcher import _advance_next_due
    from app.services.transaction_truth import apply_truth_after_transaction_update

    existing = find_confirmed(db, payment, occurrence_date)
    if existing is not None:
        return existing, False

    account = db.get(Account, payment.account_id)
    if account is None:
        return None, False

    amt = Decimal(payment.amount) if amount is None else Decimal(amount)

    txn = Transaction(
        account_id=payment.account_id,
        date=datetime.combine(occurrence_date, datetime.min.time()),
        description=(payment.description or "Scheduled payment")[:500],
        amount=amt,
        original_currency=payment.currency or account.currency or "USD",
        category_id=payment.category_id,
        is_transfer=False,
        raw_data=json.dumps({
            "source": CONFIRM_SOURCE,
            "scheduled_payment_id": payment.id,
            "occurrence_date": occurrence_date.isoformat(),
            "confirmed_at": naive_utc_now().isoformat(),
        }),
    )
    # Same inference an import would apply — the user confirmed that the
    # payment happened, not what kind of event it is.
    txn.event_type = classify_transaction(txn, account).value
    txn.classification_provenance = ClassificationProvenance.INFERRED.value
    txn.classification_confidence = 0.6

    db.add(txn)
    db.flush()

    payment.last_matched_txn_id = txn.id
    payment.last_matched_date = occurrence_date
    # Only ever move the schedule forward. Confirming a back-dated occurrence
    # must not drag next_due_date into the past and re-project everything.
    if payment.next_due_date <= occurrence_date:
        _advance_next_due(payment, occurrence_date)

    apply_truth_after_transaction_update(db, txn, None)
    return txn, True
