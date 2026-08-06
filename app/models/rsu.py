"""RSU (Restricted Stock Unit) grants and their vesting schedules.

An RSU account holds one or more *grants* (awards). Each grant vests over
time in *tranches* — a vest date and a number of units delivered on that
date. `PositionLot` can't represent this because it has no concept of a
delivery/vesting date, so RSUs get their own storage.

Valuation lives in ``app.services.rsu_service``: unvested units × the live
price of the underlying instrument (fetched from the market via
``price_service``).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RSUGrant(Base):
    __tablename__ = "rsu_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True,
    )
    instrument_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("instruments.id"), nullable=True, index=True,
    )
    # Broker's grant identifier, e.g. "26PG1BI" — used to dedupe on re-import.
    award_code: Mapped[str | None] = mapped_column(String(64), index=True)
    award_type: Mapped[str | None] = mapped_column(String(120))
    award_date: Mapped[date | None] = mapped_column(Date)
    awarded_units: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    source: Mapped[str] = mapped_column(String(40), default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account = relationship("Account")
    instrument = relationship("Instrument")
    vests = relationship(
        "RSUVest",
        back_populates="grant",
        cascade="all, delete-orphan",
        order_by="RSUVest.vest_date",
    )


class RSUVest(Base):
    __tablename__ = "rsu_vests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("rsu_grants.id"), nullable=False, index=True,
    )
    # The delivery / vesting date for this tranche.
    vest_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    units: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    # Broker's own vested/unvested split for this tranche as of the statement.
    # When None, vested status is derived from vest_date vs. today.
    units_vested: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    units_unvested: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    grant = relationship("RSUGrant", back_populates="vests")

    @property
    def is_vested(self) -> bool:
        """Vested when the broker says so, else when the date has passed."""
        if self.units_vested is not None:
            return Decimal(self.units_vested) > 0 and (
                self.units_unvested is None or Decimal(self.units_unvested) == 0
            )
        return self.vest_date <= date.today()
