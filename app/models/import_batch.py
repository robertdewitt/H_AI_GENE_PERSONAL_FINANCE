import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ImportStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportSource(str, enum.Enum):
    """Vocabulary for how a batch arrived. Stored as the *value* string.

    Kept as a plain column rather than a DB enum: SQLAlchemy's Enum type
    resolves the stored text back to a member on every read, so one row
    written with an unlisted string — as the Revolut PDF path did — turns
    every subsequent load of that batch into a LookupError. Validation
    belongs on the write side, where callers use these members.
    """
    MANUAL_UPLOAD = "manual_upload"
    AUTOMATED = "automated"
    REVOLUT_PDF = "revolut_pdf"
    MANUAL_BACKFILL = "manual_backfill"


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    source: Mapped[str] = mapped_column(
        String(30), default=ImportSource.MANUAL_UPLOAD.value
    )
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus), default=ImportStatus.PENDING
    )

    account = relationship("Account", back_populates="import_batches")
    transactions = relationship(
        "Transaction", back_populates="import_batch", cascade="all, delete-orphan"
    )
