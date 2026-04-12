"""Singleton user profile — always id=1.

Stores display preferences, personal context used by AI tax planning,
and API keys for third-party integrations.
"""
from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserProfile(Base):
    __tablename__ = "user_profile"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)

    # Display
    display_currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)

    # Personal context for AI tax planning
    country_of_residence: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(100), nullable=True)
    has_spouse: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    spouse_nationality: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Property valuation API keys
    rentcast_api_key: Mapped[str | None] = mapped_column(String(200), nullable=True)      # US — rentcast.io free tier
    property_data_api_key: Mapped[str | None] = mapped_column(String(200), nullable=True)  # UK — propertydata.co.uk
    domain_api_key: Mapped[str | None] = mapped_column(String(200), nullable=True)         # AU — domain.com.au
