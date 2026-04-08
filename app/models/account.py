import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AccountType(str, enum.Enum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    BROKERAGE = "brokerage"
    IRA = "ira"
    ROTH_IRA = "roth_ira"
    PENSION = "pension"
    FOUR_OH_ONE_K = "401k"
    REAL_ESTATE = "real_estate"
    VEHICLE = "vehicle"
    COLLECTIBLE = "collectible"
    LOAN = "loan"
    MORTGAGE = "mortgage"
    OTHER = "other"


ASSET_TYPES = {
    AccountType.CHECKING,
    AccountType.SAVINGS,
    AccountType.BROKERAGE,
    AccountType.IRA,
    AccountType.ROTH_IRA,
    AccountType.PENSION,
    AccountType.FOUR_OH_ONE_K,
    AccountType.REAL_ESTATE,
    AccountType.VEHICLE,
    AccountType.COLLECTIBLE,
    AccountType.OTHER,
}

LIABILITY_TYPES = {
    AccountType.CREDIT_CARD,
    AccountType.LOAN,
    AccountType.MORTGAGE,
}


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType), nullable=False
    )
    institution: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    is_asset: Mapped[bool] = mapped_column(Boolean, default=True)
    current_value: Mapped[float | None] = mapped_column(Float)
    value_as_of_date: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    transactions = relationship(
        "Transaction", back_populates="account", cascade="all, delete-orphan"
    )
    valuations = relationship(
        "AssetValuation", back_populates="account", cascade="all, delete-orphan"
    )
    import_batches = relationship(
        "ImportBatch", back_populates="account", cascade="all, delete-orphan"
    )

    @property
    def display_type(self) -> str:
        return self.account_type.value.replace("_", " ").title()

    @property
    def type_group(self) -> str:
        if self.account_type in {
            AccountType.CHECKING,
            AccountType.SAVINGS,
        }:
            return "Banking"
        if self.account_type in {AccountType.CREDIT_CARD}:
            return "Credit Cards"
        if self.account_type in {
            AccountType.BROKERAGE,
            AccountType.IRA,
            AccountType.ROTH_IRA,
            AccountType.PENSION,
            AccountType.FOUR_OH_ONE_K,
        }:
            return "Investments & Retirement"
        if self.account_type in {AccountType.REAL_ESTATE}:
            return "Real Estate"
        if self.account_type in {AccountType.VEHICLE}:
            return "Vehicles"
        if self.account_type in {AccountType.COLLECTIBLE}:
            return "Collectibles"
        if self.account_type in {AccountType.LOAN, AccountType.MORTGAGE}:
            return "Loans"
        return "Other"
