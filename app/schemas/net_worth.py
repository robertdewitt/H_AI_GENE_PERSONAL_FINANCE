from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class AccountBalance(BaseModel):
    account_id: int
    account_name: str
    account_type: str
    type_group: str
    balance: Decimal
    currency: str
    balance_base: Decimal | None = None
    is_asset: bool
    as_of_date: datetime


class NetWorthSnapshot(BaseModel):
    date: datetime
    currency: str
    total_assets: Decimal
    total_liabilities: Decimal
    net_worth: Decimal
    breakdown: list[AccountBalance]


class NetWorthTimeSeries(BaseModel):
    snapshots: list[NetWorthSnapshot]
