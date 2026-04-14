from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import (
    ClassificationProvenance,
    FeeTreatment,
    FxTreatmentMode,
    ReconciliationGroupType,
    ReconciliationStatus,
)


class ReconciliationGroup(Base):
    __tablename__ = "reconciliation_groups"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    group_type: Mapped[str] = mapped_column(
        String(30), default=ReconciliationGroupType.TRANSFER.value, nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30), default=ReconciliationStatus.SUGGESTED.value, nullable=False,
    )
    base_currency: Mapped[str] = mapped_column(String(10), default="USD")
    fx_treatment: Mapped[str] = mapped_column(
        String(30), default=FxTreatmentMode.NONE.value,
    )
    fee_treatment: Mapped[str] = mapped_column(
        String(30), default=FeeTreatment.EXCLUDE_FROM_NET.value,
    )
    tolerance_base: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.01"))
    residual_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    confidence: Mapped[float | None] = mapped_column(Float)
    reconciliation_confidence: Mapped[float | None] = mapped_column(Float)
    fx_rate_used: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[str | None] = mapped_column(
        String(30), default=ClassificationProvenance.INFERRED.value,
    )
    as_of_date: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(),
    )

    members = relationship(
        "ReconciliationMember",
        back_populates="group",
        cascade="all, delete-orphan",
    )


class ReconciliationMember(Base):
    __tablename__ = "reconciliation_members"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reconciliation_groups.id"), nullable=False, index=True,
    )
    transaction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transactions.id"), nullable=False, index=True,
    )

    allocated_amount_native: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    allocated_currency: Mapped[str] = mapped_column(
        String(10), nullable=False,
    )
    allocated_amount_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    role: Mapped[str | None] = mapped_column(String(30))
    is_fee_leg: Mapped[bool | None] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(),
    )

    group = relationship("ReconciliationGroup", back_populates="members")
    transaction = relationship("Transaction")
