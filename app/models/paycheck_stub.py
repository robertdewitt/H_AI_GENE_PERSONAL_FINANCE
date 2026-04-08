from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
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

    gross_pay: Mapped[float] = mapped_column(Float, nullable=False)
    net_pay: Mapped[float] = mapped_column(Float, nullable=False)

    federal_tax: Mapped[float] = mapped_column(Float, default=0.0)
    state_tax: Mapped[float] = mapped_column(Float, default=0.0)
    local_tax: Mapped[float] = mapped_column(Float, default=0.0)
    social_security: Mapped[float] = mapped_column(Float, default=0.0)
    medicare: Mapped[float] = mapped_column(Float, default=0.0)

    retirement_401k: Mapped[float] = mapped_column(Float, default=0.0)
    health_insurance: Mapped[float] = mapped_column(Float, default=0.0)
    dental_insurance: Mapped[float] = mapped_column(Float, default=0.0)
    vision_insurance: Mapped[float] = mapped_column(Float, default=0.0)
    hsa_contribution: Mapped[float] = mapped_column(Float, default=0.0)
    other_deductions: Mapped[float] = mapped_column(Float, default=0.0)

    ytd_gross: Mapped[float | None] = mapped_column(Float)
    ytd_net: Mapped[float | None] = mapped_column(Float)
    ytd_federal_tax: Mapped[float | None] = mapped_column(Float)
    ytd_state_tax: Mapped[float | None] = mapped_column(Float)
    ytd_retirement_401k: Mapped[float | None] = mapped_column(Float)

    # Raw text from OCR / CSV upload for auditing
    raw_text: Mapped[str | None] = mapped_column(Text)
    source_filename: Mapped[str | None] = mapped_column(String(500))

    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    account = relationship("Account", backref="paycheck_stubs")

    @property
    def total_taxes(self) -> float:
        return (
            self.federal_tax
            + self.state_tax
            + self.local_tax
            + self.social_security
            + self.medicare
        )

    @property
    def total_deductions(self) -> float:
        return self.gross_pay - self.net_pay

    @property
    def total_benefits(self) -> float:
        return (
            self.health_insurance
            + self.dental_insurance
            + self.vision_insurance
            + self.hsa_contribution
            + self.other_deductions
        )
