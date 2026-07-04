"""Classify scheduled payments for how they surface on the tasks page.

Two levels:

  - ``auto``     — automatic charges you don't act on (subscriptions like
                   Spotify, utility direct debits). Shown low-level.
  - ``reminder`` — payments that need attention or are expected money:
                   credit-card / loan / mortgage payments, real-estate
                   outgoings, fees, and inbound income (salary, rent).

The level can be overridden per payment via ``ScheduledPayment.flag_level``;
when unset we fall back to :func:`default_flag_level`.

Also provides :func:`find_suppressed_transfer_ids` — when a payment moves
money between two of your own accounts (e.g. Investec → Tesla Loan), we keep
only the destination (liability) side so it isn't flagged twice.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.scheduled_payment import ScheduledPayment

# Keywords that mark an asset-account outflow as a real obligation rather than
# a subscription / utility direct debit.
_REMINDER_KEYWORDS = (
    "credit card", "card payment", "mortgage", "loan", "property",
    "fee", "rent", "tax", "council", "school", "tuition", "fcu",
    "amex", "visa", "mastercard",
)

# Asset-account outflows at or above this magnitude are treated as real
# payments (not subscriptions) even without a keyword match.
_REMINDER_AMOUNT = 500.0


def default_flag_level(payment: "ScheduledPayment", account: "Account | None") -> str:
    """Best-guess level when the user hasn't set one explicitly."""
    from app.models.account import AccountType

    amt = float(payment.amount or 0)
    atype = account.account_type if account else None

    # Inbound money (salary, rent received) → reminder.
    if amt > 0:
        return "reminder"
    # Debt obligations tracked on their own account → reminder.
    if atype in (AccountType.LOAN, AccountType.MORTGAGE):
        return "reminder"
    # Anything derived from a statement (card min/balance, mortgage) → reminder.
    if (payment.source or "") == "statement":
        return "reminder"

    desc = (payment.description or "").lower()
    if atype in (AccountType.CHECKING, AccountType.SAVINGS):
        if any(kw in desc for kw in _REMINDER_KEYWORDS):
            return "reminder"
        if abs(amt) >= _REMINDER_AMOUNT:
            return "reminder"
        return "auto"

    # Charges on a credit card itself (subscriptions, membership fees) → auto.
    return "auto"


def effective_flag_level(payment: "ScheduledPayment", account: "Account | None") -> str:
    """The override if set to a known value, else the classifier default."""
    lvl = getattr(payment, "flag_level", None)
    if lvl in ("auto", "reminder"):
        return lvl
    return default_flag_level(payment, account)


def find_suppressed_transfer_ids(
    payments: list["ScheduledPayment"],
    accounts_map: dict,
) -> set[int]:
    """IDs of asset-account payments that duplicate a liability-account one.

    When the same payment exists both as an outflow from an asset account
    (the source, e.g. Investec) and as a payment on a liability account (the
    destination, e.g. Tesla Loan), we suppress the source side so the payment
    is flagged only once — on the destination account.

    Matching is by frequency + absolute amount (1-cent tolerance).
    """
    from app.models.account import AccountType, LIABILITY_TYPES

    liabilities = [
        p for p in payments
        if (a := accounts_map.get(p.account_id)) is not None
        and a.account_type in LIABILITY_TYPES
        and float(p.amount or 0) < 0
    ]
    if not liabilities:
        return set()

    suppressed: set[int] = set()
    for p in payments:
        a = accounts_map.get(p.account_id)
        if not a or a.account_type not in (AccountType.CHECKING, AccountType.SAVINGS):
            continue
        amt = abs(float(p.amount or 0))
        if amt <= 0:
            continue
        for lp in liabilities:
            if lp.frequency == p.frequency and abs(abs(float(lp.amount or 0)) - amt) <= 0.01:
                suppressed.add(p.id)
                break
    return suppressed
