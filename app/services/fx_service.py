"""Foreign-exchange rate storage, lookup, and conversion.

Rates are stored as: 1 unit of base_currency = `rate` units of quote_currency.
To convert an amount FROM quote TO base:  amount_base = amount_quote / rate
To convert an amount FROM base TO quote:  amount_quote = amount_base * rate
"""
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.currency_rate import CurrencyRate


COMMON_CURRENCIES = [
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY",
    "HKD", "SGD", "INR", "BRL", "MXN", "KRW", "SEK", "NOK",
    "DKK", "NZD", "ZAR", "THB", "TWD", "PLN", "TRY", "ILS",
]


def upsert_rate(
    db: Session,
    base_currency: str,
    quote_currency: str,
    date: datetime,
    rate: float,
    source: str = "manual",
) -> CurrencyRate:
    """Insert or update an FX rate for a given pair + date."""
    existing = db.execute(
        select(CurrencyRate).where(
            CurrencyRate.base_currency == base_currency,
            CurrencyRate.quote_currency == quote_currency,
            CurrencyRate.date == date,
        )
    ).scalar_one_or_none()

    if existing:
        existing.rate = rate
        existing.source = source
        db.commit()
        db.refresh(existing)
        return existing

    entry = CurrencyRate(
        base_currency=base_currency,
        quote_currency=quote_currency,
        date=date,
        rate=rate,
        source=source,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_rate(
    db: Session,
    from_currency: str,
    to_currency: str,
    date: datetime,
) -> float | None:
    """Retrieve the exchange rate closest to the given date.

    Returns rate such that: amount_in_to = amount_in_from * rate
    Falls back to ±30 days if exact date not found.
    """
    if from_currency == to_currency:
        return 1.0

    date_only = date.replace(hour=0, minute=0, second=0, microsecond=0)

    exact = db.execute(
        select(CurrencyRate.rate).where(
            CurrencyRate.base_currency == from_currency,
            CurrencyRate.quote_currency == to_currency,
            CurrencyRate.date == date_only,
        )
    ).scalar_one_or_none()

    if exact is not None:
        return exact

    nearby = db.execute(
        select(CurrencyRate).where(
            CurrencyRate.base_currency == from_currency,
            CurrencyRate.quote_currency == to_currency,
            CurrencyRate.date >= date_only - timedelta(days=30),
            CurrencyRate.date <= date_only + timedelta(days=30),
        ).order_by(
            CurrencyRate.date.desc()
        )
    ).scalars().all()

    if nearby:
        closest = min(
            nearby, key=lambda r: abs((r.date - date_only).total_seconds()),
        )
        return closest.rate

    # Try the inverse pair
    inverse = db.execute(
        select(CurrencyRate).where(
            CurrencyRate.base_currency == to_currency,
            CurrencyRate.quote_currency == from_currency,
            CurrencyRate.date >= date_only - timedelta(days=30),
            CurrencyRate.date <= date_only + timedelta(days=30),
        ).order_by(CurrencyRate.date.desc())
    ).scalars().all()

    if inverse:
        closest = min(
            inverse, key=lambda r: abs((r.date - date_only).total_seconds()),
        )
        return 1.0 / closest.rate if closest.rate != 0 else None

    return None


def convert_amount(
    db: Session,
    amount: float,
    from_currency: str,
    to_currency: str,
    date: datetime,
) -> tuple[float | None, float | None]:
    """Convert an amount and return (converted_amount, rate_used).

    Returns (None, None) if no rate is available.
    """
    if from_currency == to_currency:
        return amount, 1.0

    rate = get_rate(db, from_currency, to_currency, date)
    if rate is None:
        return None, None

    return round(amount * rate, 2), rate


def bulk_upsert_rates(
    db: Session,
    rates: list[dict],
) -> int:
    """Bulk insert rates. Each dict: {base, quote, date, rate, source}."""
    count = 0
    for r in rates:
        upsert_rate(
            db,
            base_currency=r["base"],
            quote_currency=r["quote"],
            date=r["date"],
            rate=r["rate"],
            source=r.get("source", "bulk_import"),
        )
        count += 1
    return count


def list_available_pairs(db: Session) -> list[dict]:
    """Return distinct currency pairs that have rates stored."""
    rows = db.execute(
        select(
            CurrencyRate.base_currency,
            CurrencyRate.quote_currency,
        ).distinct()
    ).all()
    return [
        {"base": r[0], "quote": r[1]}
        for r in rows
    ]
