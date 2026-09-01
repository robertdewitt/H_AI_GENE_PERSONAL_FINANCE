"""Scheduled payments the user deleted and does not want suggested again.

Deleting a scheduled payment used to be futile: the recurring detector and the
statement importer both rebuild payments from history, so anything removed came
straight back on the next detect or import. This table is the tombstone that
makes a deletion stick, mirroring ``DismissedDuplicate``.

Matching is on a normalised description so cosmetic differences in a payee
string ("SPOTIFY UK      LONDON" vs "SPOTIFY UK LONDON") don't let the same
payment slip back in.
"""
import re
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.clock import naive_utc_now


def normalize_description(description: str | None) -> str:
    """Lowercase, collapse runs of whitespace, strip punctuation padding."""
    return re.sub(r"\s+", " ", (description or "").strip().lower())


class DismissedScheduledPayment(Base):
    __tablename__ = "dismissed_scheduled_payments"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "description_key", name="uq_dismissed_scheduled",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True,
    )
    description_key: Mapped[str] = mapped_column(String(300), nullable=False)
    # Kept for display so the user can see what they dismissed.
    original_description: Mapped[str | None] = mapped_column(String(300))
    dismissed_at: Mapped[datetime] = mapped_column(
        default=naive_utc_now, nullable=False,
    )
