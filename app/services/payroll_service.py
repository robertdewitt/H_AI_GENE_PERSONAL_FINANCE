"""Payroll decomposition via TransactionSplit.

Supports non-cash events: employer pension contributions are representable
as splits with amount_native=0 on the cash transaction but a positive
value on a linked pension account split.
"""
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.enums import (
    ClassificationProvenance,
    EconomicEventType,
    PayrollComponent,
    SpendType,
)
from app.models.transaction import Transaction
from app.services.split_service import add_split, validate_splits, SplitValidation


@dataclass
class PayrollBreakdown:
    transaction_id: int
    components: dict[str, float] = field(default_factory=dict)
    validation: SplitValidation | None = None


def decompose_payroll(
    db: Session,
    transaction_id: int,
    components: dict[PayrollComponent, float],
    currency: str = "USD",
    provenance: str = ClassificationProvenance.IMPORTED.value,
    confidence: float | None = None,
    linked_pension_account_id: int | None = None,
) -> PayrollBreakdown:
    """Create splits for a payroll transaction.

    Components should be signed correctly:
      - salary_gross: positive (the full amount before deductions)
      - payroll_tax: negative
      - pension_contribution_employee: negative
      - net_salary_cash: positive (what hit the bank)
      - pension_contribution_employer: positive (non-cash benefit)

    The net_salary_cash split should match the transaction.amount if
    this represents the cash deposit. Employer pension contributions
    are non-cash and are created as separate splits with a linked
    pension account.
    """
    _EVENT_MAP = {
        PayrollComponent.SALARY_GROSS: EconomicEventType.PAYROLL_INCOME,
        PayrollComponent.PAYROLL_TAX: EconomicEventType.TAX_PAYMENT,
        PayrollComponent.PENSION_CONTRIBUTION_EMPLOYEE: EconomicEventType.INVESTMENT_CONTRIBUTION,
        PayrollComponent.PENSION_CONTRIBUTION_EMPLOYER: EconomicEventType.EMPLOYER_BENEFIT,
        PayrollComponent.HEALTH_BENEFIT: EconomicEventType.EMPLOYER_BENEFIT,
        PayrollComponent.WELLBEING_BENEFIT: EconomicEventType.EMPLOYER_BENEFIT,
        PayrollComponent.OTHER_DEDUCTION: EconomicEventType.FEE,
        PayrollComponent.NET_SALARY_CASH: EconomicEventType.EXTERNAL_INCOME,
    }

    _SPEND_MAP = {
        PayrollComponent.PAYROLL_TAX: SpendType.TAX,
        PayrollComponent.OTHER_DEDUCTION: SpendType.FIXED_CORE,
    }

    result = PayrollBreakdown(transaction_id=transaction_id)

    for comp, amount in components.items():
        event = _EVENT_MAP.get(comp, EconomicEventType.EXTERNAL_INCOME)
        spend = _SPEND_MAP.get(comp)
        is_spend = spend is not None

        linked_acct = None
        if comp in (
            PayrollComponent.PENSION_CONTRIBUTION_EMPLOYEE,
            PayrollComponent.PENSION_CONTRIBUTION_EMPLOYER,
        ):
            linked_acct = linked_pension_account_id

        add_split(
            db,
            transaction_id=transaction_id,
            amount_native=amount,
            currency=currency,
            event_type=event,
            spend_type=spend,
            counts_as_true_spend=is_spend,
            linked_account_id=linked_acct,
            provenance=provenance,
            confidence=confidence,
            notes=comp.value,
        )
        result.components[comp.value] = amount

    db.flush()
    result.validation = validate_splits(db, transaction_id)
    return result
