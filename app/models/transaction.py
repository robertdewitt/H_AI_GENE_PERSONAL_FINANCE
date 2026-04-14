from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import ClassificationProvenance, EconomicEventType


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_txn_date_desc", "date", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False, index=True
    )
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)

    # Amounts stored in the account's native currency
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    original_currency: Mapped[str] = mapped_column(String(10), default="USD")

    # If a foreign-currency transaction, this is the amount in the base currency
    amount_base: Mapped[float | None] = mapped_column(Float)
    exchange_rate: Mapped[float | None] = mapped_column(Float)

    balance_after: Mapped[float | None] = mapped_column(Float)

    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id")
    )
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_link_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("transfer_links.id")
    )
    import_batch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("import_batches.id")
    )

    # ── Truth layer columns ──────────────────────────────────────
    event_type: Mapped[str | None] = mapped_column(
        String(50), default=EconomicEventType.UNCLASSIFIED.value,
    )
    classification_provenance: Mapped[str | None] = mapped_column(
        String(30), default=ClassificationProvenance.IMPORTED.value,
    )
    classification_confidence: Mapped[float | None] = mapped_column(Float)

    financial_document_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("financial_documents.id"), index=True,
    )

    raw_data: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    account = relationship("Account", back_populates="transactions")
    category = relationship("Category", back_populates="transactions")
    transfer_link = relationship("TransferLink", foreign_keys=[transfer_link_id])
    import_batch = relationship("ImportBatch", back_populates="transactions")
    splits = relationship(
        "TransactionSplit", back_populates="transaction",
        cascade="all, delete-orphan",
    )
    financial_document = relationship(
        "FinancialDocument", foreign_keys=[financial_document_id],
    )

    @property
    def effective_amount(self) -> float:
        """Amount in the reporting (base) currency."""
        if self.amount_base is not None:
            return self.amount_base
        return self.amount

    CURRENCY_SYMBOLS = {
        "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
        "CAD": "C$", "AUD": "A$", "CHF": "Fr", "CNY": "¥",
        "INR": "₹", "BRL": "R$", "KRW": "₩", "SEK": "kr",
        "MXN": "$", "NZD": "NZ$",
    }

    @property
    def currency_symbol(self) -> str:
        return self.CURRENCY_SYMBOLS.get(self.original_currency, self.original_currency)

    @property
    def formatted_amount(self) -> str:
        sym = self.currency_symbol
        prefix = "+" if self.amount >= 0 else ""
        return f"{prefix}{sym}{self.amount:,.2f}"

    @property
    def formatted_amount_base(self) -> str:
        val = self.effective_amount
        prefix = "+" if val >= 0 else ""
        return f"{prefix}${val:,.2f}"

    @property
    def is_foreign_currency(self) -> bool:
        return self.original_currency != "USD" and self.exchange_rate is not None

    @property
    def is_inflow(self) -> bool:
        return self.amount >= 0
