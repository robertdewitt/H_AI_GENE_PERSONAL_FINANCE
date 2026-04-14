from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class TransactionCreate(BaseModel):
    account_id: int
    date: datetime
    description: str = Field(..., min_length=1, max_length=500)
    amount: Decimal
    original_currency: str = "USD"
    amount_base: Decimal | None = None
    exchange_rate: float | None = None
    balance_after: Decimal | None = None
    category_id: int | None = None
    is_transfer: bool = False


class TransactionResponse(BaseModel):
    id: int
    account_id: int
    date: datetime
    description: str
    amount: Decimal
    original_currency: str
    amount_base: Decimal | None
    exchange_rate: float | None
    balance_after: Decimal | None
    category_id: int | None
    is_transfer: bool
    transfer_link_id: int | None
    import_batch_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TransactionFilter(BaseModel):
    account_id: int | None = None
    category_id: int | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    search: str | None = None
    is_transfer: bool | None = None
    currency: str | None = None
