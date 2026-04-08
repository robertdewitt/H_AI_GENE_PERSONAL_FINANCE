from datetime import datetime

from pydantic import BaseModel


class PaycheckCreate(BaseModel):
    account_id: int
    pay_date: datetime
    pay_period_start: datetime | None = None
    pay_period_end: datetime | None = None
    employer: str | None = None
    currency: str = "USD"
    gross_pay: float
    net_pay: float
    federal_tax: float = 0.0
    state_tax: float = 0.0
    local_tax: float = 0.0
    social_security: float = 0.0
    medicare: float = 0.0
    retirement_401k: float = 0.0
    health_insurance: float = 0.0
    dental_insurance: float = 0.0
    vision_insurance: float = 0.0
    hsa_contribution: float = 0.0
    other_deductions: float = 0.0
    notes: str | None = None


class PaycheckResponse(BaseModel):
    id: int
    account_id: int
    pay_date: datetime
    employer: str | None
    currency: str
    gross_pay: float
    net_pay: float
    total_taxes: float
    total_deductions: float
    retirement_401k: float

    model_config = {"from_attributes": True}


class PaycheckSummary(BaseModel):
    count: int
    total_gross: float
    total_net: float
    total_taxes: float
    total_retirement: float
    total_benefits: float
