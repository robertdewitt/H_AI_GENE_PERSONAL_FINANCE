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
    MORTGAGE_PAYMENT = "mortgage_payment"
    MORTGAGE_INTEREST = "mortgage_interest"
    MORTGAGE_PRINCIPAL = "mortgage_principal"
    INVESTMENT_CONTRIBUTION = "investment_contribution"
    INVESTMENT_WITHDRAWAL = "investment_withdrawal"
    INVESTMENT_FLOW = "investment_flow"
    ASSET_FLOW = "asset_flow"
    FEE = "fee"
    TAX_PAYMENT = "tax_payment"
    PAYROLL_INCOME = "payroll_income"
    EMPLOYER_BENEFIT = "employer_benefit"
    RENTAL_INCOME = "rental_income"
    RENTAL_EXPENSE = "rental_expense"
    OWNER_DISTRIBUTION = "owner_distribution"
    DEFERRED_RENT_LIABILITY = "deferred_rent_liability"


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


class SpendType(str, enum.Enum):
    """Semantic spend classification for splits — NOT a reporting category."""
    LIFESTYLE = "lifestyle"
    FIXED_CORE = "fixed_core"
    DEBT_COST = "debt_cost"
    TAX = "tax"
    NON_SPEND_CASH_USE = "non_spend_cash_use"


class MemberRole(str, enum.Enum):
    SOURCE = "source"
    DESTINATION = "destination"
    FEE = "fee"


class PayrollComponent(str, enum.Enum):
    SALARY_GROSS = "salary_gross"
    PAYROLL_TAX = "payroll_tax"
    PENSION_CONTRIBUTION_EMPLOYEE = "pension_contribution_employee"
    PENSION_CONTRIBUTION_EMPLOYER = "pension_contribution_employer"
    HEALTH_BENEFIT = "health_benefit"
    WELLBEING_BENEFIT = "wellbeing_benefit"
    OTHER_DEDUCTION = "other_deduction"
    NET_SALARY_CASH = "net_salary_cash"


class SnapshotSource(str, enum.Enum):
    COMPUTED = "computed"
    IMPORTED = "imported"
    MANUAL = "manual"
    STALE_CARRYFORWARD = "stale_carryforward"


class DocumentType(str, enum.Enum):
    PAYROLL = "payroll"
    RENTAL_STATEMENT = "rental_statement"


class DocumentLineKind(str, enum.Enum):
    """Semantic role of a line on a structured financial document."""
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    LIABILITY = "liability"
