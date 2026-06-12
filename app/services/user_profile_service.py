"""Singleton UserProfile — always row id=1."""
from sqlalchemy.orm import Session

from app.models.user_profile import UserProfile


def get_profile(db: Session) -> UserProfile:
    profile = db.get(UserProfile, 1)
    if profile is None:
        profile = UserProfile(id=1)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def update_profile(
    db: Session,
    display_currency: str,
    country_of_residence: str | None,
    nationality: str | None,
    has_spouse: bool,
    spouse_nationality: str | None,
    rentcast_api_key: str | None = None,
    property_data_api_key: str | None = None,
    domain_api_key: str | None = None,
    recurring_stale_days: int | None = None,
    recurring_min_occurrences: int | None = None,
    recurring_min_confidence: float | None = None,
    recurring_min_amt_consistency: float | None = None,
    recurring_fixed_amt_consistency: float | None = None,
    forecast_moving_avg_months: int | None = None,
) -> UserProfile:
    profile = get_profile(db)
    profile.display_currency = display_currency.upper().strip() or "USD"
    profile.country_of_residence = country_of_residence or None
    profile.nationality = nationality or None
    profile.has_spouse = has_spouse
    profile.spouse_nationality = spouse_nationality if has_spouse else None

    # Only overwrite API keys if a non-empty value was submitted
    # (empty submission = keep existing key)
    if rentcast_api_key is not None and rentcast_api_key.strip():
        profile.rentcast_api_key = rentcast_api_key.strip()
    if property_data_api_key is not None and property_data_api_key.strip():
        profile.property_data_api_key = property_data_api_key.strip()
    if domain_api_key is not None and domain_api_key.strip():
        profile.domain_api_key = domain_api_key.strip()

    # Tuning knobs — None means "leave unchanged". Callers that want to reset
    # to defaults can pass the default value explicitly.
    if recurring_stale_days is not None:
        profile.recurring_stale_days = recurring_stale_days
    if recurring_min_occurrences is not None:
        profile.recurring_min_occurrences = recurring_min_occurrences
    if recurring_min_confidence is not None:
        profile.recurring_min_confidence = recurring_min_confidence
    if recurring_min_amt_consistency is not None:
        profile.recurring_min_amt_consistency = recurring_min_amt_consistency
    if recurring_fixed_amt_consistency is not None:
        profile.recurring_fixed_amt_consistency = recurring_fixed_amt_consistency
    if forecast_moving_avg_months is not None:
        profile.forecast_moving_avg_months = forecast_moving_avg_months

    db.commit()
    db.refresh(profile)
    return profile
