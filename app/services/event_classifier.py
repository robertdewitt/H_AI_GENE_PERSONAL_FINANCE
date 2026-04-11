"""Infer EconomicEventType from account type + transaction properties.

The classifier assigns only the narrow economic role — not reporting
nuance, which belongs to Category and PaymentDecomposition.
"""
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account, AccountType, LIABILITY_TYPES
from app.models.enums import (
    ClassificationProvenance,
    EconomicEventType,
)
from app.models.transaction import Transaction

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
