"""StockTrade — individual executed trade rows imported from IBKR or other brokers."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class StockTrade(Base):
    __tablename__ = "stock_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True,
    )
    instrument_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.id"), nullable=False, index=True,
    )
    trade_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # positive = buy, negative = sell
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    proceeds: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    commission: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    source: Mapped[str] = mapped_column(String(40), default="ibkr")
    # Idempotent re-import dedup key — unique per account
    ibkr_dedup_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account = relationship("Account")
    instrument = relationship("Instrument")
