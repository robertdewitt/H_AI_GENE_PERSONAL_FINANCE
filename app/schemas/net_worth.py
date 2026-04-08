from datetime import datetime

from pydantic import BaseModel


class AccountBalance(BaseModel):
    account_id: int
    account_name: str
    account_type: str
    type_group: str
    balance: float
    currency: str
    balance_base: float | None = None
    is_asset: bool
    as_of_date: datetime


class NetWorthSnapshot(BaseModel):
    date: datetime
    currency: str
    total_assets: float
    total_liabilities: float
    net_worth: float
    breakdown: list[AccountBalance]


class NetWorthTimeSeries(BaseModel):
    snapshots: list[NetWorthSnapshot]
