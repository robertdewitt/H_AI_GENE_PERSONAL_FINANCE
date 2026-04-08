from datetime import datetime

from pydantic import BaseModel, Field

from app.models.account import AccountType


class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    account_type: AccountType
    institution: str | None = None
    currency: str = "USD"
    is_asset: bool = True
    current_value: float | None = None
    value_as_of_date: datetime | None = None
    notes: str | None = None


class AccountUpdate(BaseModel):
    name: str | None = None
    account_type: AccountType | None = None
    institution: str | None = None
    currency: str | None = None
    is_asset: bool | None = None
    current_value: float | None = None
    value_as_of_date: datetime | None = None
    notes: str | None = None


class AccountResponse(BaseModel):
    id: int
    name: str
    account_type: AccountType
    institution: str | None
    currency: str
    is_asset: bool
    current_value: float | None
    value_as_of_date: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
