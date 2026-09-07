"""A transaction that probably settled a scheduled payment, awaiting a decision.

Matching used to be binary: either a transaction cleared every gate and the
schedule advanced, or nothing happened at all — silently. That is how a
mortgage sat "overdue by 65 days" with three payments for it sitting on the
ledger: one gate (the sign) failed, and "no match" is indistinguishable from
"nothing happened yet".

A score has a middle. Strong evidence still settles the schedule outright;
weak evidence is ignored as before; and the band between the two lands here,
where it is visible and one click from resolved instead of invisible forever.
"""
from datetime import datetime

from sqlalchemy import (
    DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"


class ScheduledMatchProposal(Base):
    __tablename__ = "scheduled_match_proposals"
    __table_args__ = (
        UniqueConstraint(
            "scheduled_payment_id", "transaction_id",
            name="uq_scheduled_match_proposal",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheduled_payment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scheduled_payments.id"), nullable=False, index=True,
    )
    transaction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transactions.id"), nullable=False, index=True,
    )

    score: Mapped[float] = mapped_column(Float, nullable=False)
    # Per-signal breakdown, so the page can say *why* rather than showing a
    # bare number the user has no way to judge.
    components: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20), default=STATUS_PENDING, nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    scheduled_payment = relationship("ScheduledPayment")
    transaction = relationship("Transaction")
