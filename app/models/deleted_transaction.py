"""Soft-delete log — written before every hard delete so rows are recoverable."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeletedTransaction(Base):
    __tablename__ = "deleted_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Original transaction pk (informational; not a FK since the row is gone)
    original_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    # Core transaction fields
    account_id: Mapped[int] = mapped_column(nullable=False, index=True)
    date: Mapped[datetime] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    original_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_transfer: Mapped[bool] = mapped_column(default=False)
    category_id: Mapped[int | None] = mapped_column(nullable=True)
    import_batch_id: Mapped[int | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    deleted_at: Mapped[datetime] = mapped_column(default=datetime.now, nullable=False)
