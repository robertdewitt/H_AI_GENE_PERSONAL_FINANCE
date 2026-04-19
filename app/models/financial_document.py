"""Structured multi-line financial documents (payslips, rental statements).

Lines are stored normalized for querying and time-series aggregation.
Splits on a parent transaction reference lines via document_line_id.
"""
import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FinancialDocument(Base):
    __tablename__ = "financial_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True,
    )
    rental_property_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("rental_properties.id"), index=True,
    )
    statement_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime)
    period_end: Mapped[datetime | None] = mapped_column(DateTime)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    reference: Mapped[str | None] = mapped_column(String(300))
    employer_or_counterparty: Mapped[str | None] = mapped_column(String(300))
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    split_validation_ok: Mapped[bool | None] = mapped_column(Boolean)
    provenance: Mapped[str | None] = mapped_column(String(30))
    confidence: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    lines = relationship(
        "FinancialDocumentLine",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="FinancialDocumentLine.line_order",
    )
    account = relationship("Account", foreign_keys=[account_id])
    rental_property = relationship("RentalProperty", foreign_keys=[rental_property_id])


class FinancialDocumentLine(Base):
    __tablename__ = "financial_document_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("financial_documents.id"), nullable=False, index=True,
    )
    line_order: Mapped[int] = mapped_column(Integer, default=0)
    line_kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    component_code: Mapped[str | None] = mapped_column(String(80), index=True)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    amount_native: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    is_cash: Mapped[bool] = mapped_column(Boolean, default=True)
    rental_property_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("rental_properties.id"),
    )
    extra_json: Mapped[str | None] = mapped_column(Text)

    document = relationship("FinancialDocument", back_populates="lines")

    def extra_dict(self) -> dict:
        if not self.extra_json:
            return {}
        try:
            return json.loads(self.extra_json)
        except json.JSONDecodeError:
            return {}


class PropertyPnLSnapshot(Base):
    """Time-series rollup of rental property activity for a statement period."""

    __tablename__ = "property_pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rental_property_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("rental_properties.id"), nullable=False, index=True,
    )
    financial_document_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("financial_documents.id"),
    )
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    statement_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    total_income: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    total_expense: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    owner_draw: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    liability_adjustment: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    net_operating_income: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    net_cash_flow: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    source: Mapped[str] = mapped_column(String(40), default="document")
    confidence: Mapped[float | None] = mapped_column(Float)
    stale_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
