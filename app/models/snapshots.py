"""Point-in-time snapshot models for the MTM time-series engine.

Every snapshot stores the best-known value even if stale, and carries
source, confidence, and staleness metadata so consumers never mistake
stale data for live precision.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, func, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AccountBalanceSnapshot(Base):
    __tablename__ = "account_balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True,
    )
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    value_native: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    value_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    fx_rate: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    stale_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AssetValuationSnapshot(Base):
    __tablename__ = "asset_valuation_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True,
    )
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    value_native: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    value_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    fx_rate: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    stale_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LiabilityBalanceSnapshot(Base):
    __tablename__ = "liability_balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True,
    )
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    value_native: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    value_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    fx_rate: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    stale_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class HouseholdSnapshot(Base):
    __tablename__ = "household_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    total_assets_base: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_liabilities_base: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    net_worth_base: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(10), default="USD")
    accounts_included: Mapped[int] = mapped_column(Integer, default=0)
    stale_accounts: Mapped[int] = mapped_column(Integer, default=0)
    low_confidence_accounts: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
