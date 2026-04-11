from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import ClassificationProvenance, PaymentComponent


class PaymentDecomposition(Base):
    __tablename__ = "payment_decompositions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    transaction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transactions.id"), nullable=False, index=True,
    )
    component: Mapped[str] = mapped_column(
        String(30), nullable=False,
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_base: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[str] = mapped_column(
        String(30), default=ClassificationProvenance.IMPORTED.value,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(),
    )

    transaction = relationship("Transaction")
