"""Per-user profile.

Stores display preferences, personal context used by AI tax planning,
API keys for third-party integrations, and the recurring/forecast
detection knobs. Prior to multi-user support this was a singleton
(``id=1``); now each user has one row keyed by ``user_id``.
"""
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# Defaults for the tunable detection / forecast knobs. The same defaults
# back-fill the columns when the profile row is created and act as fallbacks
# when a column happens to be NULL on an upgraded database.
RECURRING_STALE_DAYS_DEFAULT          = 120
RECURRING_MIN_OCCURRENCES_DEFAULT     = 3
RECURRING_MIN_CONFIDENCE_DEFAULT      = 0.50
RECURRING_MIN_AMT_CONSISTENCY_DEFAULT = 0.30
RECURRING_FIXED_AMT_CONSISTENCY_DEFAULT = 0.85
FORECAST_MOVING_AVG_MONTHS_DEFAULT    = 6


class UserProfile(Base):
    __tablename__ = "user_profile"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_profile_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
    )

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

    # Recurring-payment detection tuning. Read by recurring_detector at every
    # detection run, so changes take effect immediately.
    recurring_stale_days: Mapped[int | None] = mapped_column(
        Integer, default=RECURRING_STALE_DAYS_DEFAULT, nullable=True,
    )
    recurring_min_occurrences: Mapped[int | None] = mapped_column(
        Integer, default=RECURRING_MIN_OCCURRENCES_DEFAULT, nullable=True,
    )
    recurring_min_confidence: Mapped[float | None] = mapped_column(
        Float, default=RECURRING_MIN_CONFIDENCE_DEFAULT, nullable=True,
    )
    recurring_min_amt_consistency: Mapped[float | None] = mapped_column(
        Float, default=RECURRING_MIN_AMT_CONSISTENCY_DEFAULT, nullable=True,
    )
    recurring_fixed_amt_consistency: Mapped[float | None] = mapped_column(
        Float, default=RECURRING_FIXED_AMT_CONSISTENCY_DEFAULT, nullable=True,
    )

    # Cash-flow forecast tuning — number of months used by the trailing-mean
    # projection for variable-amount scheduled payments.
    forecast_moving_avg_months: Mapped[int | None] = mapped_column(
        Integer, default=FORECAST_MOVING_AVG_MONTHS_DEFAULT, nullable=True,
    )
