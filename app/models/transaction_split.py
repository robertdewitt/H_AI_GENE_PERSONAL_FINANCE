"""TransactionSplit — semantic allocation of a raw transaction row.

A single transaction can have multiple splits. Each split carries its own
event_type, spend_type, currency, provenance, and confidence. The sum of
split amounts must equal the parent transaction amount (enforced at
service layer, not DB constraint).
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TransactionSplit(Base):
    __tablename__ = "transaction_splits"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    transaction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transactions.id"), nullable=False, index=True,
    )

    amount_native: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_base: Mapped[float | None] = mapped_column(Float)
    fx_rate: Mapped[float | None] = mapped_column(Float)

    event_type: Mapped[str | None] = mapped_column(String(50))
    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id"),
    )

    linked_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id"),
    )
    linked_reconciliation_group_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reconciliation_groups.id"),
    )
    document_line_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("financial_document_lines.id"), index=True,
    )

    counts_as_true_spend: Mapped[bool] = mapped_column(Boolean, default=False)
    spend_type: Mapped[str | None] = mapped_column(String(30))

    provenance: Mapped[str | None] = mapped_column(String(30))
    confidence: Mapped[float | None] = mapped_column(Float)
    as_of_date: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(),
    )

    transaction = relationship("Transaction", back_populates="splits")
    category = relationship("Category")
    linked_account = relationship("Account", foreign_keys=[linked_account_id])
    document_line = relationship("FinancialDocumentLine", foreign_keys=[document_line_id])
