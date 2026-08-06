"""Scheduled / recurring payments and expected future obligations.

Sources:
  auto_detected  — inferred from repeated transaction patterns
  statement      — extracted from a statement (e.g. mortgage next-payment)
  manual         — user-entered

Frequency values: weekly | biweekly | monthly | quarterly | annually | once
Amount type:      fixed | estimated | variable
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey,
    Integer, Numeric, String, Text, func,
    ForeignKey,
    Integer,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ScheduledPayment(Base):
    __tablename__ = "scheduled_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )

    # ── What ─────────────────────────────────────────────────────────────
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    # positive = inflow (income/refund), negative = outflow (bill/payment)
    amount_type: Mapped[str] = mapped_column(String(20), default="fixed")
    # fixed | estimated | variable
    currency: Mapped[str] = mapped_column(String(10), default="USD")

    # ── Where ─────────────────────────────────────────────────────────────
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True
    )
    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id"), nullable=True
    )

    # ── When ──────────────────────────────────────────────────────────────
    frequency: Mapped[str] = mapped_column(String(20), default="monthly")
    # weekly | biweekly | monthly | quarterly | annually | once
    next_due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # anchor day for monthly payments (1–31); None = use next_due_date day

    # ── Provenance ────────────────────────────────────────────────────────
    source: Mapped[str] = mapped_column(String(20), default="manual")
    # auto_detected | statement | manual
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Flag level ──────────────────────────────────────────────────────────
    # How this payment surfaces on the tasks page:
    #   "auto"     → automatic charge (subscription / direct debit), low-level
    #   "reminder" → needs attention (card/loan/mortgage payment, income in)
    #   None       → fall back to the classifier default (payment_classifier)
    flag_level: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ── Status ────────────────────────────────────────────────────────────
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Matching ──────────────────────────────────────────────────────────
    last_matched_txn_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("transactions.id", use_alter=True), nullable=True
    )
    last_matched_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # ── Relationships ─────────────────────────────────────────────────────
    account = relationship("Account")
    category = relationship("Category")
    last_matched_txn = relationship(
        "Transaction", foreign_keys=[last_matched_txn_id]
    )
