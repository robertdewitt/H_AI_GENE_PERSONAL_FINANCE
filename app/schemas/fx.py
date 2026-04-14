from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class FXRateCreate(BaseModel):
    base_currency: str
    quote_currency: str
    date: datetime
    rate: float
    source: str = "manual"


class FXRateResponse(BaseModel):
    id: int
    base_currency: str
    quote_currency: str
    date: datetime
    rate: float
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class FXConvertRequest(BaseModel):
    amount: Decimal
    from_currency: str
    to_currency: str
    date: datetime


class FXConvertResponse(BaseModel):
    original_amount: Decimal
    converted_amount: Decimal | None
    rate: float | None
    from_currency: str
    to_currency: str
    date: datetime
