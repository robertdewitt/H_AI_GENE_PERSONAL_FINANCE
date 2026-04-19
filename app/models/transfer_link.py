from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TransferLink(Base):
    __tablename__ = "transfer_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_transaction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transactions.id"), nullable=False
    )
    to_transaction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transactions.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    from_transaction = relationship(
        "Transaction",
        foreign_keys=[from_transaction_id],
        backref="outgoing_transfer",
    )
    to_transaction = relationship(
        "Transaction",
        foreign_keys=[to_transaction_id],
        backref="incoming_transfer",
    )
