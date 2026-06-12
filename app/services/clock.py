"""Timezone-aware now/staleness helpers.

The rest of the codebase mostly used bare ``datetime.now()`` which is
locally-naive — fine for a single-timezone deployment but fragile when
two values cross the locale boundary (e.g. an import that wrote times
in TZ A is compared against a snapshot generated in TZ B). All instant-
in-time values produced inside the app now go through ``utc_now()``.

SQLite returns ``DateTime`` columns as locally-naive Python datetimes
regardless of what we wrote in. To compare those against ``utc_now()``
(aware UTC) without a TypeError, strip the tzinfo at the boundary with
``to_naive_utc()``.

Note: this module deliberately does NOT touch *calendar-date* values
(``Transaction.date``, ``ScheduledPayment.next_due_date``, etc.). Those
are user-facing dates pulled from statements and carry no time zone.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def naive_utc_now() -> datetime:
    """Return the current instant as a *naive* UTC datetime.

    Use this when the result will immediately be compared against a value
    that came back from SQLite (which is always naive in Python). Keeping
    both sides naive avoids ``can't compare offset-naive and offset-aware``
    TypeErrors without losing the UTC anchor.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-aware datetime to a naive UTC one.

    * ``None`` passes through unchanged.
    * Aware datetimes are converted to UTC and have their tzinfo stripped.
    * Naive datetimes are assumed to already be in UTC and returned as-is.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
