from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PaycheckStub(Base):
    """Parsed paycheck / pay stub data."""

    __tablename__ = "paycheck_stubs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True
    )
    pay_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    pay_period_start: Mapped[datetime | None] = mapped_column(DateTime)
    pay_period_end: Mapped[datetime | None] = mapped_column(DateTime)
    employer: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(10), default="USD")

    gross_pay: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    net_pay: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    federal_tax: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    state_tax: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    local_tax: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    social_security: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    medicare: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))

    retirement_401k: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    health_insurance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    dental_insurance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    vision_insurance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    hsa_contribution: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    other_deductions: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))

    ytd_gross: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    ytd_net: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    ytd_federal_tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    ytd_state_tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    ytd_retirement_401k: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    # Raw text from OCR / CSV upload for auditing
    raw_text: Mapped[str | None] = mapped_column(Text)
    source_filename: Mapped[str | None] = mapped_column(String(500))

    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    account = relationship("Account", backref="paycheck_stubs")

    @property
    def total_taxes(self) -> Decimal:
        return (
            self.federal_tax
            + self.state_tax
            + self.local_tax
            + self.social_security
            + self.medicare
        )

    @property
    def total_deductions(self) -> Decimal:
        return self.gross_pay - self.net_pay

    @property
    def total_benefits(self) -> Decimal:
        return (
            self.health_insurance
            + self.dental_insurance
            + self.vision_insurance
            + self.hsa_contribution
            + self.other_deductions
        )
