"""Infer EconomicEventType from account type + transaction properties.

The classifier assigns only the narrow economic role — not reporting
nuance, which belongs to Category and PaymentDecomposition.

Users work with categories only.  Economic event types are system-derived
and are used internally to auto-populate split spend metadata.
"""
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account, AccountType, LIABILITY_TYPES
from app.models.enums import (
    ClassificationProvenance,
    EconomicEventType,
    SpendType,
)
from app.models.transaction import Transaction


# ── Spend metadata mapping ───────────────────────────────────────────
# Maps each EconomicEventType to (SpendType, counts_as_true_spend).
# Used to auto-populate split metadata so users never need to set these
# directly — they only pick a category.

_SPEND_METADATA: dict[str, tuple[str, bool]] = {
    EconomicEventType.UNCLASSIFIED.value:              (SpendType.LIFESTYLE.value, True),
    EconomicEventType.EXTERNAL_INCOME.value:           (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.PAYROLL_INCOME.value:            (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.EMPLOYER_BENEFIT.value:          (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.LIFESTYLE_EXPENSE.value:         (SpendType.LIFESTYLE.value, True),
    EconomicEventType.CARD_PURCHASE.value:             (SpendType.LIFESTYLE.value, True),
    EconomicEventType.INTERNAL_TRANSFER.value:         (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.CARD_PAYMENT_SETTLEMENT.value:   (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.LIABILITY_PAYMENT.value:         (SpendType.DEBT_COST.value, True),
    EconomicEventType.MORTGAGE_PAYMENT.value:          (SpendType.DEBT_COST.value, True),
    EconomicEventType.MORTGAGE_INTEREST.value:         (SpendType.DEBT_COST.value, True),
    EconomicEventType.MORTGAGE_PRINCIPAL.value:        (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.INVESTMENT_CONTRIBUTION.value:   (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.INVESTMENT_WITHDRAWAL.value:     (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.INVESTMENT_FLOW.value:           (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.ASSET_FLOW.value:                (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.FEE.value:                       (SpendType.DEBT_COST.value, True),
    EconomicEventType.TAX_PAYMENT.value:               (SpendType.TAX.value, True),
    EconomicEventType.RENTAL_INCOME.value:             (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.RENTAL_EXPENSE.value:            (SpendType.LIFESTYLE.value, True),
    EconomicEventType.OWNER_DISTRIBUTION.value:        (SpendType.NON_SPEND_CASH_USE.value, False),
    EconomicEventType.DEFERRED_RENT_LIABILITY.value:   (SpendType.NON_SPEND_CASH_USE.value, False),
}


def event_type_to_spend_metadata(event_type: str | None) -> tuple[str, bool]:
    """Map an event_type string to (spend_type, counts_as_true_spend).

    Called when creating splits from category-only user input so that
    spend analysis always has correct metadata without user involvement.
    Defaults to (LIFESTYLE, True) for unknown/null event types — conservative,
    treats ambiguous transactions as real spend.
    """
    if event_type is None:
        return SpendType.LIFESTYLE.value, True
    return _SPEND_METADATA.get(event_type, (SpendType.LIFESTYLE.value, True))

log = logging.getLogger(__name__)

INVESTMENT_ACCOUNT_TYPES = {
    AccountType.BROKERAGE,
    AccountType.IRA,
    AccountType.ROTH_IRA,
    AccountType.PENSION,
    AccountType.FOUR_OH_ONE_K,
}

ASSET_ONLY_TYPES = {
    AccountType.REAL_ESTATE,
    AccountType.VEHICLE,
    AccountType.COLLECTIBLE,
}

PAYMENT_KEYWORDS = {
    "payment", "pymt", "pmt", "autopay", "auto pay",
    "thank you", "online pmt", "ach payment",
}

_FEE_PHRASES = {
    "service charge", "annual fee", "late fee", "interest charge",
    "overdraft fee", "maintenance fee", "atm fee", "wire fee",
    "monthly fee", "account fee",
}
_FEE_WORD_RE = re.compile(r"\bfee\b", re.IGNORECASE)

_TAX_PHRASES = {"tax payment", "estimated tax", "irs", "hmrc"}
_TAX_WORD_RE = re.compile(r"\btax\b", re.IGNORECASE)

_INTEREST_RE = re.compile(r"\binterest\b", re.IGNORECASE)

_PAYROLL_KEYWORDS = {
    "payroll", "salary", "wages", "direct deposit",
    "net pay", "employer",
}


def classify_transaction(txn: Transaction, account: Account) -> EconomicEventType:
    """Pure function: derive event_type from txn + account context."""
    desc_lower = txn.description.lower()
    acct_type = account.account_type

    if txn.is_transfer or txn.transfer_link_id is not None:
        return EconomicEventType.INTERNAL_TRANSFER

    # Account-type-specific classification takes priority
    if acct_type == AccountType.CREDIT_CARD:
        if txn.amount > 0:
            return EconomicEventType.CARD_PAYMENT_SETTLEMENT
        return EconomicEventType.CARD_PURCHASE

    if acct_type == AccountType.MORTGAGE:
        if txn.amount > 0:
            return EconomicEventType.MORTGAGE_PAYMENT
        if _INTEREST_RE.search(desc_lower):
            return EconomicEventType.MORTGAGE_INTEREST
        return EconomicEventType.FEE

    if acct_type == AccountType.LOAN:
        if txn.amount > 0:
            return EconomicEventType.LIABILITY_PAYMENT
        return EconomicEventType.FEE

    if acct_type in INVESTMENT_ACCOUNT_TYPES:
        if txn.amount > 0:
            return EconomicEventType.INVESTMENT_CONTRIBUTION
        return EconomicEventType.INVESTMENT_WITHDRAWAL

    if acct_type in ASSET_ONLY_TYPES:
        return EconomicEventType.ASSET_FLOW

    # Keyword-based classification for banking accounts
    if (any(p in desc_lower for p in _FEE_PHRASES)
            or _FEE_WORD_RE.search(desc_lower)):
        return EconomicEventType.FEE

    if (any(p in desc_lower for p in _TAX_PHRASES)
            or _TAX_WORD_RE.search(desc_lower)):
        return EconomicEventType.TAX_PAYMENT

    if txn.amount >= 0:
        if any(kw in desc_lower for kw in _PAYROLL_KEYWORDS):
            return EconomicEventType.PAYROLL_INCOME
        return EconomicEventType.EXTERNAL_INCOME

    return EconomicEventType.LIFESTYLE_EXPENSE


def classify_batch(
    db: Session,
    transaction_ids: list[int] | None = None,
    provenance: ClassificationProvenance = ClassificationProvenance.INFERRED,
) -> int:
    """Classify a set of transactions (or all unclassified ones).

    Returns number of transactions updated.
    """
    query = select(Transaction)
    if transaction_ids:
        query = query.where(Transaction.id.in_(transaction_ids))
    else:
        query = query.where(
            (Transaction.event_type.is_(None))
            | (Transaction.event_type == EconomicEventType.UNCLASSIFIED.value)
        )

    txns = db.execute(query).scalars().all()
    if not txns:
        return 0

    account_cache: dict[int, Account] = {}
    count = 0
    for txn in txns:
        if txn.account_id not in account_cache:
            account_cache[txn.account_id] = db.get(Account, txn.account_id)
        acct = account_cache[txn.account_id]
        if acct is None:
            continue
        event = classify_transaction(txn, acct)
        txn.event_type = event.value
        txn.classification_provenance = provenance.value
        txn.classification_confidence = 0.6 if provenance == ClassificationProvenance.INFERRED else 1.0
        count += 1

    db.flush()
    return count
