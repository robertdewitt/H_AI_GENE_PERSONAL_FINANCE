from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class TransferCandidate(BaseModel):
    from_transaction_id: int
    to_transaction_id: int
    amount: Decimal
    date: datetime
    confidence: float
    from_account_name: str
    to_account_name: str
    from_description: str
    to_description: str
    from_currency: str = "USD"
    to_currency: str = "USD"


class TransferLinkCreate(BaseModel):
    from_transaction_id: int
    to_transaction_id: int


class TransferLinkResponse(BaseModel):
    id: int
    from_transaction_id: int
    to_transaction_id: int
    amount: Decimal
    date: datetime
    confidence: float
    confirmed_by_user: bool
    created_at: datetime

    model_config = {"from_attributes": True}
