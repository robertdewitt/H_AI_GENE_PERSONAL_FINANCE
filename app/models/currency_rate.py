from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CurrencyRate(Base):
    """Historical exchange rates for FX conversion.

    Stores one row per (base, quote, date) triple.
    rate = how many units of quote_currency per 1 unit of base_currency.
    e.g. base=USD, quote=EUR, rate=0.92 means 1 USD = 0.92 EUR.
    """

    __tablename__ = "currency_rates"
    __table_args__ = (
        UniqueConstraint(
            "base_currency", "quote_currency", "date",
            name="uq_rate_pair_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_currency: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    quote_currency: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
