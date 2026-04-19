"""Security / instrument identifiers and position lots — foundation for brokerage truth.

Lot-level cost basis and price history are stored separately from account-level
AssetValuation for household rollup.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    asset_class: Mapped[str | None] = mapped_column(String(40))
    cusip: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    positions = relationship("PositionLot", back_populates="instrument")
    prices = relationship("PriceSnapshot", back_populates="instrument")


class PositionLot(Base):
    __tablename__ = "position_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True,
    )
    instrument_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.id"), nullable=False, index=True,
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    cost_basis_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    confidence: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account = relationship("Account")
    instrument = relationship("Instrument", back_populates="positions")


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.id"), nullable=False, index=True,
    )
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    source: Mapped[str] = mapped_column(String(40), default="manual")
    confidence: Mapped[float | None] = mapped_column(Float)
    stale_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    instrument = relationship("Instrument", back_populates="prices")
