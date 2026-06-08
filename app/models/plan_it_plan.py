"""Plan-It instalment-plan rows extracted from an Amex BA statement.

One row per active instalment plan as of the most recent statement upload.
The full set for an account is replaced on every import so completed plans
fall off naturally and counters (instalment N/M, remaining balance) reflect
the latest snapshot.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PlanItPlan(Base):
    __tablename__ = "plan_it_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True
    )

    # Identification
    start_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(500), nullable=False)

    # Lifetime values
    plan_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    plan_total_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    balance_remaining: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    # This month's payment breakdown
    monthly_plan_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    monthly_fee: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    monthly_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    # Progress
    instalment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    instalment_total: Mapped[int] = mapped_column(Integer, nullable=False)

    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
