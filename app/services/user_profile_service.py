"""Singleton UserProfile — always row id=1.

Third-party API keys (rentcast / property_data / domain) are stored
encrypted-at-rest and decrypted transparently on read via
:func:`get_profile`. Writes go through :func:`update_profile` which
encrypts before commit. Direct ORM mutation bypasses encryption — don't
do that.
"""
from sqlalchemy.orm import Session
from sqlalchemy.orm import attributes as _attrs

from app.models.user_profile import UserProfile
from app.services.secret_box import decrypt as _decrypt, encrypt as _encrypt, is_encrypted as _is_enc


_ENCRYPTED_FIELDS = ("rentcast_api_key", "property_data_api_key", "domain_api_key")


def _decrypt_in_place(profile: UserProfile) -> UserProfile:
    """Mark each encrypted column's plaintext as the already-saved value
    so the ORM doesn't think the row is dirty after we substitute.
    """
    for f in _ENCRYPTED_FIELDS:
        stored = getattr(profile, f, None)
        if _is_enc(stored):
            _attrs.set_committed_value(profile, f, _decrypt(stored))
    return profile


def get_profile(db: Session) -> UserProfile:
    profile = db.get(UserProfile, 1)
    if profile is None:
        profile = UserProfile(id=1)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return _decrypt_in_place(profile)


def encrypt_all_plaintext_api_keys(db: Session) -> int:
    """One-shot migration: encrypt any API-key columns still in plaintext.

    Returns the number of rows touched. Safe to call on every startup —
    rows that are already encrypted are skipped.
    """
    profile = db.get(UserProfile, 1)
    if profile is None:
        return 0
    changed = 0
    for f in _ENCRYPTED_FIELDS:
        stored = getattr(profile, f, None)
        if stored and not _is_enc(stored):
            setattr(profile, f, _encrypt(stored))
            changed += 1
    if changed:
        db.commit()
    return changed


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
    # (empty submission = keep existing key). Encrypt before persisting.
    if rentcast_api_key is not None and rentcast_api_key.strip():
        profile.rentcast_api_key = _encrypt(rentcast_api_key.strip())
    if property_data_api_key is not None and property_data_api_key.strip():
        profile.property_data_api_key = _encrypt(property_data_api_key.strip())
    if domain_api_key is not None and domain_api_key.strip():
        profile.domain_api_key = _encrypt(domain_api_key.strip())

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
    return _decrypt_in_place(profile)
