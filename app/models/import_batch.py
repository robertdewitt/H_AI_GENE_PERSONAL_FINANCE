import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ImportStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportSource(str, enum.Enum):
    MANUAL_UPLOAD = "manual_upload"
    AUTOMATED = "automated"


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    source: Mapped[ImportSource] = mapped_column(
        Enum(ImportSource), default=ImportSource.MANUAL_UPLOAD
    )
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus), default=ImportStatus.PENDING
    )

    account = relationship("Account", back_populates="import_batches")
    transactions = relationship(
        "Transaction", back_populates="import_batch", cascade="all, delete-orphan"
    )
