"""Detect recurring payments from transaction history.

Algorithm per account:
1. Group transactions by normalized description (lowercase, strip digits/punctuation).
2. For groups with 3+ occurrences, compute intervals between consecutive dates.
3. Classify frequency if median interval is within tolerance of known periods.
4. Compute confidence from (interval consistency) × (amount consistency).
5. Project next_due_date from the most recent occurrence.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Tolerance: how many days either side of a period's ideal interval qualifies
_FREQ_BUCKETS = [
    ("weekly",     7,   3),
    ("biweekly",   14,  4),
    ("monthly",    30,  8),
    ("quarterly",  91,  12),
    ("annually",   365, 20),
]

_MIN_OCCURRENCES   = 3    # need at least this many to infer a pattern
_MIN_CONFIDENCE    = 0.50
_MIN_AMT_CONSISTENCY = 0.30  # below this the amounts are too erratic to be
                              # the same merchant; above this we still emit
                              # a suggestion but tag it as `variable` so the
                              # forecast uses a moving average instead of a
                              # frozen median (e.g. salary, credit-card bills)
_FIXED_AMT_CONSISTENCY = 0.85  # at/above this the amount is effectively fixed
_STALE_DAYS = 120  # ~4 months — drop suggestions for merchants we haven't
                    # seen recently, they've usually been cancelled


def _normalize(description: str) -> str:
    """Strip digits, collapse whitespace, lowercase — keeps the merchant name."""
    s = re.sub(r"\d+", "", description or "")
    s = re.sub(r"[^a-zA-Z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _classify_frequency(intervals: list[int]) -> tuple[str, float] | None:
    """Return (frequency, regularity_score) or None if no pattern found."""
    if not intervals:
        return None
    median = statistics.median(intervals)
    for freq, ideal, tol in _FREQ_BUCKETS:
        if abs(median - ideal) <= tol:
            # Regularity = fraction of intervals within tolerance of the median
            within = sum(1 for x in intervals if abs(x - median) <= tol)
            regularity = within / len(intervals)
            return freq, regularity
    return None


def _amount_consistency(amounts: list[Decimal]) -> float:
    """1.0 if all the same, lower if variable."""
    if len(set(amounts)) == 1:
        return 1.0
    floats = [float(a) for a in amounts]
    mean = statistics.mean(floats)
    if mean == 0:
        return 0.5
    stdev = statistics.stdev(floats) if len(floats) > 1 else 0.0
    cv = stdev / abs(mean)          # coefficient of variation
    return max(0.0, 1.0 - cv)       # 0 CV → 1.0, high CV → 0.0


def _next_due(last_date: date, frequency: str, day_of_month: int | None = None) -> date:
    """Project one period forward from last_date."""
    today = date.today()
    candidates = {
        "weekly":    last_date + timedelta(days=7),
        "biweekly":  last_date + timedelta(days=14),
        "monthly":   _add_months(last_date, 1, day_of_month),
        "quarterly": _add_months(last_date, 3, day_of_month),
        "annually":  _add_months(last_date, 12, day_of_month),
        "once":      last_date,
    }
    nxt = candidates.get(frequency, last_date + timedelta(days=30))
    # If already past, advance until it's in the future
    while nxt < today and frequency != "once":
        if frequency == "weekly":
            nxt += timedelta(days=7)
        elif frequency == "biweekly":
            nxt += timedelta(days=14)
        elif frequency == "monthly":
            nxt = _add_months(nxt, 1, day_of_month)
        elif frequency == "quarterly":
            nxt = _add_months(nxt, 3, day_of_month)
        elif frequency == "annually":
            nxt = _add_months(nxt, 12, day_of_month)
        else:
            break
    return nxt


def _add_months(d: date, months: int, anchor_day: int | None = None) -> date:
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = anchor_day or d.day
    # Clamp to valid day (e.g. Feb 30 → Feb 28)
    import calendar
    max_day = calendar.monthrange(year, month)[1]
    day = min(day, max_day)
    return date(year, month, day)


def detect_recurring_payments(db: "Session") -> list[dict]:
    """Scan all transaction history and return recurring payment suggestions.

    Each suggestion dict:
      account_id, account_name, description, amount, currency,
      frequency, next_due_date, day_of_month, confidence,
      occurrences, last_seen
    """
    from sqlalchemy import select
    from app.models.account import Account
    from app.models.transaction import Transaction

    txns = db.execute(
        select(Transaction)
        .where(Transaction.is_transfer.is_(False))
        .order_by(Transaction.account_id, Transaction.date)
    ).scalars().all()

    accounts = {
        a.id: a for a in db.execute(select(Account)).scalars().all()
    }

    today = date.today()

    # Bucket: (account_id, normalized_desc) → list of (date, amount, currency)
    buckets: dict[tuple, list] = defaultdict(list)
    for txn in txns:
        key = (txn.account_id, _normalize(txn.description or ""))
        if not key[1]:
            continue
        d = txn.date.date() if hasattr(txn.date, "date") else txn.date
        buckets[key].append((d, txn.amount, txn.original_currency or ""))

    suggestions = []
    for (acct_id, norm_desc), entries in buckets.items():
        if len(entries) < _MIN_OCCURRENCES:
            continue

        entries.sort(key=lambda x: x[0])
        dates   = [e[0] for e in entries]
        amounts = [e[1] for e in entries]

        intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
        result = _classify_frequency(intervals)
        if result is None:
            continue

        freq, regularity = result
        amt_score = _amount_consistency(amounts)
        # For variable-amount streams (salary, credit-card bills) the rhythm
        # is the signal — confidence is regularity alone, with a small penalty
        # so they rank below clean fixed payments.
        if amt_score >= _FIXED_AMT_CONSISTENCY:
            confidence = round(regularity * amt_score, 3)
            amount_type = "fixed"
        else:
            confidence = round(regularity * 0.85, 3)
            amount_type = "variable"

        if amt_score < _MIN_AMT_CONSISTENCY:
            continue  # amounts too erratic to call it the same merchant
        if confidence < _MIN_CONFIDENCE:
            continue

        last_date  = dates[-1]

        # Stale guard — drop suggestions for merchants we haven't seen for
        # more than ~4 months. They've usually been cancelled and shouldn't
        # show up as "upcoming" payments.
        if (today - last_date).days > _STALE_DAYS:
            continue

        if amount_type == "variable":
            # 6-month trailing mean — the same number the forecast will use
            # so the user sees the right expected amount at confirmation time.
            cutoff = today - timedelta(days=183)
            recent = [float(a) for d, a in zip(dates, amounts) if d >= cutoff]
            avg_amt = (
                statistics.mean(recent) if recent
                else statistics.mean([float(a) for a in amounts])
            )
            display_amt = Decimal(str(round(avg_amt, 2)))
        else:
            display_amt = Decimal(str(round(
                statistics.median([float(a) for a in amounts]), 2
            )))

        dom        = last_date.day if freq in ("monthly", "quarterly", "annually") else None
        currency   = entries[-1][2] or (accounts[acct_id].currency if acct_id in accounts else "USD")

        acct = accounts.get(acct_id)
        suggestions.append({
            "account_id":    acct_id,
            "account_name":  acct.name if acct else f"Account {acct_id}",
            "description":   entries[-1][0].strftime("%Y-%m-%d"),  # placeholder
            "raw_description": _first_description(db, acct_id, norm_desc),
            "amount":        display_amt,
            "amount_type":   amount_type,
            "currency":      currency,
            "frequency":     freq,
            "next_due_date": _next_due(last_date, freq, dom).isoformat(),
            "day_of_month":  dom,
            "confidence":    confidence,
            "occurrences":   len(entries),
            "last_seen":     last_date.isoformat(),
        })
        # Use the actual description, not the date
        suggestions[-1]["description"] = suggestions[-1]["raw_description"]

    # Sort by confidence descending
    suggestions.sort(key=lambda s: -s["confidence"])
    return suggestions


def _first_description(db: "Session", account_id: int, norm_desc: str) -> str:
    """Return the most recent real description matching this normalized key."""
    from sqlalchemy import select
    from app.models.transaction import Transaction

    txns = db.execute(
        select(Transaction.description)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.date.desc())
        .limit(200)
    ).scalars().all()

    for d in txns:
        if _normalize(d or "") == norm_desc:
            return d or norm_desc
    return norm_desc
