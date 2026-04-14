from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class PaycheckCreate(BaseModel):
    account_id: int
    pay_date: datetime
    pay_period_start: datetime | None = None
    pay_period_end: datetime | None = None
    employer: str | None = None
    currency: str = "USD"
    gross_pay: Decimal
    net_pay: Decimal
    federal_tax: Decimal = Decimal("0.00")
    state_tax: Decimal = Decimal("0.00")
    local_tax: Decimal = Decimal("0.00")
    social_security: Decimal = Decimal("0.00")
    medicare: Decimal = Decimal("0.00")
    retirement_401k: Decimal = Decimal("0.00")
    health_insurance: Decimal = Decimal("0.00")
    dental_insurance: Decimal = Decimal("0.00")
    vision_insurance: Decimal = Decimal("0.00")
    hsa_contribution: Decimal = Decimal("0.00")
    other_deductions: Decimal = Decimal("0.00")
    notes: str | None = None


class PaycheckResponse(BaseModel):
    id: int
    account_id: int
    pay_date: datetime
    employer: str | None
    currency: str
    gross_pay: Decimal
    net_pay: Decimal
    total_taxes: Decimal
    total_deductions: Decimal
    retirement_401k: Decimal

    model_config = {"from_attributes": True}


class PaycheckSummary(BaseModel):
    count: int
    total_gross: Decimal
    total_net: Decimal
    total_taxes: Decimal
    total_retirement: Decimal
    total_benefits: Decimal
