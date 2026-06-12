"""Learned categorization rules — stores user corrections so the system
gets better over time without needing the LLM for repeat patterns."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CategoryRule(Base):
    __tablename__ = "category_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )
    pattern: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), default="user_correction")
    hit_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    category = relationship("Category")
