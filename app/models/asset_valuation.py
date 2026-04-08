from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AssetValuation(Base):
    __tablename__ = "asset_valuations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True
    )
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    source: Mapped[str] = mapped_column(String(50), default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    account = relationship("Account", back_populates="valuations")
