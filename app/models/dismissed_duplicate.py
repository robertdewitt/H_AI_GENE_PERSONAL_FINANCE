"""Records duplicate groups the user has explicitly dismissed as not-a-duplicate."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DismissedDuplicate(Base):
    __tablename__ = "dismissed_duplicates"
    __table_args__ = (
        UniqueConstraint("account_id", "txn_date", "amount", name="uq_dismissed_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(nullable=False, index=True)
    txn_date: Mapped[str] = mapped_column(nullable=False)   # stored as YYYY-MM-DD string
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(
        default=lambda: __import__("app.services.clock", fromlist=["naive_utc_now"]).naive_utc_now(),
        nullable=False,
    )
