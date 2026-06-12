import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BalanceTruthSource, LiabilityBalanceSource


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
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    value_as_of_date: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    # Real estate / physical asset fields
    property_address: Mapped[str | None] = mapped_column(String(500))
    purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    purchase_date: Mapped[datetime | None] = mapped_column(DateTime)
    linked_mortgage_account_id: Mapped[int | None] = mapped_column(Integer)

    # ── Truth layer columns ──────────────────────────────────────
    balance_truth_source: Mapped[str | None] = mapped_column(
        String(30), default=BalanceTruthSource.TRANSACTION_SUM.value,
    )
    liability_balance_source: Mapped[str | None] = mapped_column(
        String(40),
    )
    statement_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    statement_balance_as_of: Mapped[datetime | None] = mapped_column(DateTime)
    original_principal_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    interest_rate: Mapped[float | None] = mapped_column(Float)        # annual rate, e.g. 0.0425 = 4.25%
    monthly_payment: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    balance_confidence: Mapped[float | None] = mapped_column(Float)
    balance_stale_hint: Mapped[bool | None] = mapped_column(Boolean)
    liability_balance_stale: Mapped[bool | None] = mapped_column(Boolean)

    # Plan-It / instalment-plan outstanding balance (Amex BA, etc.) — total
    # remaining principal across active plans, separate from the revolving
    # statement_balance. Surfaced in the UI as a sub-balance so the user
    # sees future committed obligations.
    plan_it_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    plan_it_as_of: Mapped[datetime | None] = mapped_column(DateTime)

    # Overdraft facility (Investec, UK banks). overdraft_limit is the agreed
    # facility; the running balance below zero is what's currently drawn.
    overdraft_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    overdraft_as_of: Mapped[datetime | None] = mapped_column(DateTime)

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

    CURRENCY_SYMBOLS = {
        "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
        "CAD": "C$", "AUD": "A$", "CHF": "Fr", "CNY": "¥",
        "INR": "₹", "BRL": "R$", "KRW": "₩", "SEK": "kr",
        "MXN": "$", "NZD": "NZ$",
    }

    @property
    def currency_symbol(self) -> str:
        return self.CURRENCY_SYMBOLS.get(self.currency, self.currency)

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
