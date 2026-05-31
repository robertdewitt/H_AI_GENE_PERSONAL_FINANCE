"""Real estate historical HPI lookup.

Estimates annual appreciation for a property by fetching regional House
Price Index data from publicly available sources (no API key required):

  - US states  → FRED All-Transactions HPI (FHFA), e.g. CASTHPI
  - US national → FRED USSTHPI
  - UK          → FRED GBRHPIQISMEI (BIS/OECD residential prices)

Returns the compound annual growth rate from the most recent 5-10 years
of index data.  Falls back to 4.0 % if the fetch fails, and sets
``flagged = True`` with a descriptive reason.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

_FALLBACK_RATE = 0.04   # 4 % annual

# ── FRED state-level series IDs (All-Transactions HPI, FHFA) ─────────────────
_US_STATE_SERIES: dict[str, str] = {
    "alabama":        "ALSTHPI",  "alaska":           "AKSTHPI",
    "arizona":        "AZSTHPI",  "arkansas":         "ARSTHPI",
    "california":     "CASTHPI",  "colorado":         "COSTHPI",
    "connecticut":    "CTSTHPI",  "delaware":         "DESTHPI",
    "florida":        "FLSTHPI",  "georgia":          "GASTHPI",
    "hawaii":         "HISTHPI",  "idaho":            "IDSTHPI",
    "illinois":       "ILSTHPI",  "indiana":          "INSTHPI",
    "iowa":           "IASTHPI",  "kansas":           "KSSTHPI",
    "kentucky":       "KYSTHPI",  "louisiana":        "LASTHPI",
    "maine":          "MESTHPI",  "maryland":         "MDSTHPI",
    "massachusetts":  "MASTHPI",  "michigan":         "MISTHPI",
    "minnesota":      "MNSTHPI",  "mississippi":      "MSSTHPI",
    "missouri":       "MOSTHPI",  "montana":          "MTSTHPI",
    "nebraska":       "NESTHPI",  "nevada":           "NVSTHPI",
    "new hampshire":  "NHSTHPI",  "new jersey":       "NJSTHPI",
    "new mexico":     "NMSTHPI",  "new york":         "NYSTHPI",
    "north carolina": "NCSTHPI",  "north dakota":     "NDSTHPI",
    "ohio":           "OHSTHPI",  "oklahoma":         "OKSTHPI",
    "oregon":         "ORSTHPI",  "pennsylvania":     "PASTHPI",
    "rhode island":   "RISTHPI",  "south carolina":   "SCSTHPI",
    "south dakota":   "SDSTHPI",  "tennessee":        "TNSTHPI",
    "texas":          "TXSTHPI",  "utah":             "UTSTHPI",
    "vermont":        "VTSTHPI",  "virginia":         "VASTHPI",
    "washington":     "WASTHPI",  "west virginia":    "WVSTHPI",
    "wisconsin":      "WISTHPI",  "wyoming":          "WYSTHPI",
    "district of columbia": "DCSTHPI",
}

_US_NATIONAL_SERIES = "USSTHPI"
_UK_SERIES          = "GBRHPIQISMEI"

_FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class HPIResult:
    annual_return: float     # e.g. 0.043 = 4.3 % per year
    source: str              # human-readable description
    years_of_data: int       # how many years were used to compute the rate
    flagged: bool
    flag_reason: str | None


# ── Address parsing ───────────────────────────────────────────────────────────

def _detect_country(address: str) -> str:
    """Return 'UK' or 'US' (or 'unknown')."""
    low = address.lower()
    if any(x in low for x in ("united kingdom", " uk,", ", uk", "england",
                               "scotland", "wales")):
        return "UK"
    # UK postcode pattern e.g. SE13 5GA, SW1A 1AA
    if re.search(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s+\d[A-Z]{2}\b", address):
        return "UK"
    if any(x in low for x in ("united states", ", usa", " usa,", "u.s.a")):
        return "US"
    # US zip code
    if re.search(r"\b\d{5}(?:-\d{4})?\b", address):
        return "US"
    return "unknown"


def _extract_us_state(address: str) -> str | None:
    low = address.lower()
    for state in _US_STATE_SERIES:
        if state in low:
            return state
    # Two-letter abbreviations — match against the ORIGINAL (cased) string so
    # English words like "in" / "or" / "hi" do not trigger Indiana / Oregon /
    # Hawaii. Real US addresses use uppercase state codes.
    abbrevs = {
        "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
        "CA": "california", "CO": "colorado", "CT": "connecticut",
        "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii",
        "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
        "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine",
        "MD": "maryland", "MA": "massachusetts", "MI": "michigan",
        "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
        "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new hampshire",
        "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
        "NC": "north carolina", "ND": "north dakota", "OH": "ohio",
        "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
        "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
        "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
        "VA": "virginia", "WA": "washington", "WV": "west virginia",
        "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
    }
    for abbr, full in abbrevs.items():
        # Require comma-or-space boundary before, and space + ZIP / comma /
        # end-of-string after — i.e. the conventional "City, ST 12345" form.
        if re.search(r"(?:,|\s)" + abbr + r"(?:\s+\d{5}|,|\s*$)", address):
            return full
    return None


# ── FRED data fetch ───────────────────────────────────────────────────────────

def _fetch_fred_csv(series_id: str, timeout: int = 8) -> list[tuple[datetime, float]]:
    """Download a FRED series CSV and return (date, value) pairs sorted by date."""
    import requests
    url = _FRED_CSV_URL.format(series_id=series_id)
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    rows: list[tuple[datetime, float]] = []
    for line in resp.text.splitlines()[1:]:   # skip header
        parts = line.strip().split(",")
        if len(parts) < 2 or parts[1].strip() == ".":
            continue
        try:
            date  = datetime.strptime(parts[0].strip(), "%Y-%m-%d")
            value = float(parts[1].strip())
            rows.append((date, value))
        except (ValueError, IndexError):
            continue
    return sorted(rows, key=lambda r: r[0])


def _cagr_from_series(
    rows: list[tuple[datetime, float]],
    max_years: int = 10,
    min_years: int = 3,
) -> tuple[float, int] | None:
    """Compute CAGR from the tail of an HPI series.

    Returns (cagr, years_used) or None if not enough data.
    """
    if len(rows) < 2:
        return None

    end_date, end_val = rows[-1]
    # Find start point ≈ max_years back, no less than min_years back
    target_start = datetime(end_date.year - max_years, end_date.month, 1)
    min_start    = datetime(end_date.year - min_years, end_date.month, 1)

    # Find closest row to target_start (from below)
    start_row = None
    for dt, val in rows:
        if dt >= target_start:
            start_row = (dt, val)
            break
    if start_row is None:
        # Use earliest available if the series is shorter than max_years
        start_row = rows[0]

    start_date, start_val = start_row
    if start_date >= end_date or start_val <= 0 or end_val <= 0:
        return None

    years = (end_date - start_date).days / 365.25
    if years < min_years:
        return None

    cagr = (end_val / start_val) ** (1 / years) - 1
    return cagr, int(years)


# ── Public API ────────────────────────────────────────────────────────────────

def get_hpi_for_address(address: str | None) -> HPIResult:
    """Return historical HPI CAGR for the given property address.

    Tries regional data first, falls back to national, then to 4 % default.
    """
    if not address or not address.strip():
        return HPIResult(
            annual_return=_FALLBACK_RATE,
            source="default (no address)",
            years_of_data=0,
            flagged=True,
            flag_reason="No property address set — using default 4 %/yr appreciation",
        )

    country = _detect_country(address)

    # ── UK ────────────────────────────────────────────────────────────────────
    if country == "UK":
        try:
            rows = _fetch_fred_csv(_UK_SERIES)
            result = _cagr_from_series(rows)
            if result:
                cagr, years = result
                return HPIResult(
                    annual_return=round(cagr, 4),
                    source=f"FRED {_UK_SERIES} (UK residential, BIS/OECD, {years}yr CAGR)",
                    years_of_data=years,
                    flagged=False,
                    flag_reason=None,
                )
        except Exception as exc:
            log.warning("UK HPI fetch failed: %s", exc)
        return HPIResult(
            annual_return=_FALLBACK_RATE,
            source="default (UK fetch failed)",
            years_of_data=0,
            flagged=True,
            flag_reason=f"Could not fetch UK HPI data — using default {_FALLBACK_RATE*100:.0f}%/yr",
        )

    # ── US ────────────────────────────────────────────────────────────────────
    if country == "US":
        state = _extract_us_state(address)
        # Try state-level first
        if state and state in _US_STATE_SERIES:
            series_id = _US_STATE_SERIES[state]
            try:
                rows = _fetch_fred_csv(series_id)
                result = _cagr_from_series(rows)
                if result:
                    cagr, years = result
                    return HPIResult(
                        annual_return=round(cagr, 4),
                        source=f"FRED {series_id} ({state.title()} HPI, FHFA, {years}yr CAGR)",
                        years_of_data=years,
                        flagged=False,
                        flag_reason=None,
                    )
            except Exception as exc:
                log.warning("US state HPI fetch failed (%s): %s", series_id, exc)

        # Fall back to national
        try:
            rows = _fetch_fred_csv(_US_NATIONAL_SERIES)
            result = _cagr_from_series(rows)
            if result:
                cagr, years = result
                region_note = f", {state.title()} state data unavailable" if state else ""
                return HPIResult(
                    annual_return=round(cagr, 4),
                    source=f"FRED {_US_NATIONAL_SERIES} (US national HPI, FHFA, {years}yr CAGR{region_note})",
                    years_of_data=years,
                    flagged=state is not None,   # flag if we wanted state but got national
                    flag_reason=(
                        f"State-level data unavailable; using US national HPI"
                        if state else None
                    ),
                )
        except Exception as exc:
            log.warning("US national HPI fetch failed: %s", exc)

        return HPIResult(
            annual_return=_FALLBACK_RATE,
            source="default (US fetch failed)",
            years_of_data=0,
            flagged=True,
            flag_reason=f"Could not fetch US HPI data — using default {_FALLBACK_RATE*100:.0f}%/yr",
        )

    # ── Unknown country ───────────────────────────────────────────────────────
    return HPIResult(
        annual_return=_FALLBACK_RATE,
        source="default (country not recognised)",
        years_of_data=0,
        flagged=True,
        flag_reason=f"Could not determine country from address — using default {_FALLBACK_RATE*100:.0f}%/yr",
    )
