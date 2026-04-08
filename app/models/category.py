import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CategoryType(str, enum.Enum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    INVESTMENT = "investment"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id")
    )
    category_type: Mapped[CategoryType] = mapped_column(
        Enum(CategoryType), nullable=False
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    parent = relationship("Category", remote_side="Category.id", backref="children")
    transactions = relationship("Transaction", back_populates="category")
