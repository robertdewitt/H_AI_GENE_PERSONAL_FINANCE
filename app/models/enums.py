"""Shared enums for the financial truth layer.

EconomicEventType is intentionally narrow — economic role of a raw
transaction row, not reporting nuance (that stays in Category).
"""
import enum


class EconomicEventType(str, enum.Enum):
    UNCLASSIFIED = "unclassified"
    EXTERNAL_INCOME = "external_income"
    LIFESTYLE_EXPENSE = "lifestyle_expense"
    INTERNAL_TRANSFER = "internal_transfer"
    CARD_PURCHASE = "card_purchase"
    CARD_PAYMENT_SETTLEMENT = "card_payment_settlement"
    LIABILITY_PAYMENT = "liability_payment"
    INVESTMENT_FLOW = "investment_flow"
    ASSET_FLOW = "asset_flow"
    FEE = "fee"
    TAX_PAYMENT = "tax_payment"


class ClassificationProvenance(str, enum.Enum):
    IMPORTED = "imported"
    INFERRED = "inferred"
    USER_CONFIRMED = "user_confirmed"
    RULE_DERIVED = "rule_derived"


class BalanceTruthSource(str, enum.Enum):
    TRANSACTION_SUM = "transaction_sum"
    LATEST_STATEMENT = "latest_statement"
    LATEST_VALUATION = "latest_valuation"
    LIABILITY_BALANCE = "liability_balance"
    MANUAL_MARK = "manual_mark"
    HYBRID = "hybrid"


class LiabilityBalanceSource(str, enum.Enum):
    STATEMENT_BALANCE = "statement_balance"
    IMPORTED_PRINCIPAL_BALANCE = "imported_principal_balance"
    USER_MARK = "user_mark"
    DERIVED_ESTIMATE = "derived_estimate"


class ReconciliationStatus(str, enum.Enum):
    SUGGESTED = "suggested"
    MATCHED = "matched"
    PARTIALLY_MATCHED = "partially_matched"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SPLIT_REQUIRED = "split_required"


class ReconciliationGroupType(str, enum.Enum):
    TRANSFER = "transfer"
    CARD_SETTLEMENT = "card_settlement"
    LOAN_PAYMENT = "loan_payment"
    SPLIT = "split"


class FeeTreatment(str, enum.Enum):
    EXCLUDE_FROM_NET = "exclude_from_net"
    INCLUDE_IN_NET = "include_in_net"
    SEPARATE_LINE = "separate_line"


class FxTreatmentMode(str, enum.Enum):
    NONE = "none"
    SPOT_ON_GROUP_DATE = "spot_on_group_date"
    MEMBER_RATES = "member_rates"
    EXPLICIT_GROUP_RATE = "explicit_group_rate"


class PaymentComponent(str, enum.Enum):
    PRINCIPAL = "principal"
    INTEREST = "interest"
    ESCROW = "escrow"
    INSURANCE = "insurance"
    TAX = "tax"
    FEE = "fee"
    OTHER = "other"
