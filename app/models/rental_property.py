"""Rental property entity — links statements, P&L, and optional asset account."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RentalProperty(Base):
    __tablename__ = "rental_properties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(64), index=True)
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id"), index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(),
    )

    account = relationship("Account", foreign_keys=[account_id])
