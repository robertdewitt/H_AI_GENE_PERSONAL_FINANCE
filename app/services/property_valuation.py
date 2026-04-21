"""Property value estimation — routes to the right API by country/currency.

Provider matrix:
  US  → Rentcast AVM  (api.rentcast.io)         — free tier 50 req/mo, API key optional
  UK  → PropertyData  (api.propertydata.co.uk)   — paid, ~£15/mo, requires key
  UK  → HM Land Registry fallback               — free, no key, last-sold price only
  AU  → Domain API    (api.domain.com.au)        — free dev tier, requires key
  *   → None (caller shows manual-entry prompt)

Country detection order:
  1. account.currency  (GBP → UK, AUD → AU, USD/CAD/etc → US)
  2. user profile country_of_residence
"""
from __future__ import annotations

import json as _json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

# How many seconds before we give up on an API call
_TIMEOUT = 10


@dataclass
class PropertyEstimate:
    value: float
    source: str          # e.g. "rentcast", "hm_land_registry", "propertydata", "domain"
    source_label: str    # human-readable label
    is_estimate: bool    # False = last-sold price (not a live AVM)
    notes: str = ""


def estimate_all_providers(
    address: str,
    currency: str,
    country_of_residence: str | None = None,
    rentcast_api_key: str | None = None,
    property_data_api_key: str | None = None,
    domain_api_key: str | None = None,
) -> list[PropertyEstimate]:
    """Try ALL applicable providers and return every estimate that succeeds.

    Returns a list so the caller can present all options to the user.
    """
    region = _detect_region(currency, country_of_residence)
    log.info("Property lookup (all providers): region=%s address=%r", region, address)
    results: list[PropertyEstimate] = []

    if region == "US":
        r = _rentcast(address, rentcast_api_key)
        if r:
            results.append(r)

    elif region == "UK":
        if property_data_api_key:
            r = _property_data_uk(address, property_data_api_key)
            if r:
                results.append(r)
        r = _hm_land_registry(address)
        if r:
            results.append(r)
            # Offer an HPI-adjusted forward estimate alongside the raw last-sold
            adj = _hpi_adjusted_estimate(r)
            if adj:
                results.append(adj)

    elif region == "AU":
        if domain_api_key:
            r = _domain_au(address, domain_api_key)
            if r:
                results.append(r)

    return results


def estimate_property_value(
    address: str,
    currency: str,
    country_of_residence: str | None = None,
    rentcast_api_key: str | None = None,
    property_data_api_key: str | None = None,
    domain_api_key: str | None = None,
) -> PropertyEstimate | None:
    """Route to the right provider and return an estimate, or None."""
    results = estimate_all_providers(
        address, currency, country_of_residence,
        rentcast_api_key, property_data_api_key, domain_api_key,
    )
    return results[0] if results else None


def provider_status(
    currency: str,
    country_of_residence: str | None,
    rentcast_api_key: str | None,
    property_data_api_key: str | None,
    domain_api_key: str | None,
) -> dict:
    """Return info about which provider will be used and whether a key is needed."""
    region = _detect_region(currency, country_of_residence)

    if region == "US":
        return {
            "region": "US",
            "provider": "Rentcast",
            "key_required": False,
            "key_present": bool(rentcast_api_key),
            "notes": "Free tier: 50 lookups/month. Register at rentcast.io for an API key to increase limits.",
            "links": [("rentcast.io", "https://www.rentcast.io/api")],
        }
    if region == "UK":
        if property_data_api_key:
            return {
                "region": "UK",
                "provider": "PropertyData",
                "key_required": True,
                "key_present": True,
                "notes": "Live AVM estimate via propertydata.co.uk.",
                "links": [("propertydata.co.uk", "https://propertydata.co.uk/api")],
            }
        return {
            "region": "UK",
            "provider": "HM Land Registry (last sold price)",
            "key_required": False,
            "key_present": True,
            "notes": (
                "No paid key found — using HM Land Registry last-sold price as a baseline. "
                "This is the price last paid for the property, not a current market estimate. "
                "Add a PropertyData API key for a live AVM."
            ),
            "links": [
                ("propertydata.co.uk", "https://propertydata.co.uk/api"),
                ("Zoopla", "https://www.zoopla.co.uk"),
                ("Rightmove", "https://www.rightmove.co.uk"),
            ],
        }
    if region == "AU":
        return {
            "region": "AU",
            "provider": "Domain API" if domain_api_key else "None",
            "key_required": True,
            "key_present": bool(domain_api_key),
            "notes": (
                "Domain.com.au API key required for Australian property estimates. "
                "Free developer tier available."
                if not domain_api_key
                else "Live estimate via Domain.com.au API."
            ),
            "links": [("domain.com.au developer", "https://developer.domain.com.au")],
        }
    return {
        "region": "Other",
        "provider": "None",
        "key_required": True,
        "key_present": False,
        "notes": "No automated valuation service is available for this region. Enter the value manually.",
        "links": [],
    }


# ── Region detection ───────────────────────────────────────────────────────────

_CURRENCY_TO_REGION = {
    "USD": "US", "CAD": "US",  # treat CAD as US for now
    "GBP": "UK",
    "AUD": "AU",
}

_COUNTRY_TO_REGION = {
    "United States": "US",
    "Canada": "US",
    "United Kingdom": "UK",
    "Australia": "AU",
}


def _detect_region(currency: str, country: str | None) -> str:
    if country and country in _COUNTRY_TO_REGION:
        return _COUNTRY_TO_REGION[country]
    return _CURRENCY_TO_REGION.get(currency.upper(), "US")


# ── Rentcast (US) ──────────────────────────────────────────────────────────────

def _rentcast(address: str, api_key: str | None) -> PropertyEstimate | None:
    import urllib.error
    try:
        encoded = urllib.parse.quote(address)
        url = f"https://api.rentcast.io/v1/avm/value?address={encoded}"
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-Api-Key"] = api_key
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = _json.loads(resp.read())
            # Rentcast returns: {"price": N, "priceRangeLow": N, "priceRangeHigh": N}
            price = data.get("price") or data.get("priceRangeLow") or data.get("value")
            if price:
                return PropertyEstimate(
                    value=float(price),
                    source="rentcast",
                    source_label="Rentcast AVM",
                    is_estimate=True,
                )
            log.warning("Rentcast: response missing price field for %r: %s", address, data)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        log.warning("Rentcast HTTP %s for %r: %s", e.code, address, body)
    except Exception as e:
        log.warning("Rentcast failed for %r: %s", address, e)
    return None


# ── HM Land Registry (UK, free fallback) ──────────────────────────────────────

_UK_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}[0-9][0-9A-Z]?\s*[0-9][A-Z]{2})\b", re.IGNORECASE
)


_UK_NON_STREET = re.compile(
    r"\b(london|england|uk|united kingdom|wales|scotland|"
    r"[a-z]+ ?(borough|city|county|district))\b",
    re.IGNORECASE,
)

def _extract_street(address: str) -> str | None:
    """Pull just the street name (no house number, no city/country) from a UK address."""
    # Remove postcode
    cleaned = _UK_POSTCODE_RE.sub("", address).strip().rstrip(",").strip()
    # Split on commas — street is usually the first comma-delimited part
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    first = parts[0] if parts else cleaned
    # Strip leading house number (digits / digits+letter)
    first = re.sub(r"^\d+[A-Za-z]?\s+", "", first).strip()
    # Remove city / country noise words
    first = _UK_NON_STREET.sub("", first).strip()
    # Keep only the first 3 words — street names are rarely longer
    words = first.split()[:3]
    result = " ".join(words).strip()
    return result.upper() if result else None


def _fetch_hm_ppd(postcode: str, street: str | None, page_size: int = 20) -> list:
    """Fetch Price Paid Data transactions. Filters by street name if provided."""
    encoded_postcode = urllib.parse.quote(postcode, safe="")
    url = (
        "https://landregistry.data.gov.uk/data/ppi/transaction-record.json"
        f"?propertyAddress.postcode={encoded_postcode}&_pageSize={page_size}"
    )
    if street:
        url += f"&propertyAddress.street={urllib.parse.quote(street, safe='')}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = _json.loads(resp.read())
    return data.get("result", {}).get("items", [])


_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_PPD_DATE_RE = re.compile(
    r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})",
    re.IGNORECASE,
)

def _parse_ppd_date(item: dict) -> tuple:
    """Return (year, month, day) tuple for sorting — falls back to (0,0,0)."""
    date_str = item.get("transactionDate", "")
    m = _PPD_DATE_RE.search(date_str)
    if m:
        day, mon, year = int(m.group(1)), _MONTH_MAP[m.group(2).lower()], int(m.group(3))
        return (year, mon, day)
    return (0, 0, 0)


def _hm_land_registry(address: str) -> PropertyEstimate | None:
    """Look up the most recent transaction price for a UK address via
    the HM Land Registry Price Paid Data API (free, no key required).

    First tries street + postcode for an exact-address match; falls back
    to postcode-only if nothing is found at street level.
    """
    match = _UK_POSTCODE_RE.search(address)
    if not match:
        log.debug("HM Land Registry: no postcode found in %r", address)
        return None

    postcode = match.group(1).upper().strip()
    street = _extract_street(address)

    try:
        items: list = []
        # 1. Try narrow street+postcode search first
        if street:
            items = _fetch_hm_ppd(postcode, street, page_size=10)
            if items:
                log.info("HM Land Registry: %d result(s) for street=%r postcode=%s", len(items), street, postcode)

        # 2. Fall back to postcode-only
        if not items:
            items = _fetch_hm_ppd(postcode, None, page_size=10)
            if items:
                log.info("HM Land Registry: %d result(s) for postcode=%s (no street filter)", len(items), postcode)

        if not items:
            log.warning("HM Land Registry: no results for %s / %s", street, postcode)
            return None

        # Prefer residential property types (D=detached, S=semi, T=terraced, F=flat)
        # propertyType may be a dict {"@id": "...URI.../Detached"} or a plain string
        _RESIDENTIAL = {"detached", "semi-detached", "terraced", "flat-maisonette"}
        def _prop_type_str(item: dict) -> str:
            pt = item.get("propertyType") or ""
            if isinstance(pt, dict):
                # Linked-data response uses _about (URI) or prefLabel
                labels = pt.get("prefLabel") or []
                if labels and isinstance(labels, list):
                    pt = labels[0].get("_value", "")
                else:
                    pt = pt.get("_about") or pt.get("@id") or pt.get("value") or ""
            return str(pt).lower()

        residential = [i for i in items if any(rt in _prop_type_str(i) for rt in _RESIDENTIAL)]
        items = residential if residential else items

        items_sorted = sorted(items, key=_parse_ppd_date, reverse=True)
        latest = items_sorted[0]
        price = latest.get("pricePaid")
        if price:
            date_str = latest.get("transactionDate", "unknown date")
            # Derive a human-readable sold-year for the HPI adjustment
            sold_year = _sold_year(date_str)
            label = f"Last sold: {date_str}"
            if street:
                label = f"{street} — last sold: {date_str}"
            result = PropertyEstimate(
                value=float(price),
                source="hm_land_registry",
                source_label="HM Land Registry (last sold)",
                is_estimate=False,
                notes=label,
            )
            # Also return an HPI-adjusted estimate alongside
            return result
    except Exception as e:
        log.warning("HM Land Registry failed for %s / %s: %s", street, postcode, e)
    return None


def _sold_year(date_str: str) -> int | None:
    """Extract 4-digit year from an RFC 2822 or ISO date string."""
    m = re.search(r"\b(19|20)\d{2}\b", date_str)
    return int(m.group(0)) if m else None


def _uk_hpi_index(year: int, month: int) -> float | None:
    """Fetch the UK-average house price index for a given year/month from
    the Land Registry Linked Data service. Returns the index value or None.
    """
    month_str = f"{year}-{month:02d}"
    url = (
        f"https://landregistry.data.gov.uk/data/ukhpi/region/united-kingdom"
        f"/month/{month_str}.json"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = _json.loads(resp.read())
        topic = data.get("result", {}).get("primaryTopic", {})
        idx = topic.get("indicesSASM") or topic.get("index") or topic.get("housePriceIndex")
        return float(idx) if idx else None
    except Exception as e:
        log.debug("UK HPI lookup failed for %s: %s", month_str, e)
        return None


def _hpi_adjusted_estimate(base: PropertyEstimate) -> PropertyEstimate | None:
    """Apply UK national HPI growth to project a past sale price to today.

    Fetches the UKHPI index at time of sale and the most recent available
    month, then scales the sold price by the ratio.
    """
    import datetime as _dt

    sold_year = _sold_year(base.notes or "")
    if not sold_year:
        return None

    # Approximate sold month from notes; default to June if not parseable
    sold_month = 6
    m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", base.notes or "", re.I)
    if m:
        sold_month = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }.get(m.group(0).lower(), 6)

    sale_hpi = _uk_hpi_index(sold_year, sold_month)
    if not sale_hpi:
        return None

    # Try recent months for a current index (land registry lags ~2 months)
    now = _dt.date.today()
    current_hpi = None
    for delta in range(0, 5):
        y = now.year
        mo = now.month - delta
        if mo <= 0:
            mo += 12
            y -= 1
        current_hpi = _uk_hpi_index(y, mo)
        if current_hpi:
            break

    if not current_hpi:
        return None

    adjusted = base.value * (current_hpi / sale_hpi)
    growth_pct = (current_hpi / sale_hpi - 1) * 100
    return PropertyEstimate(
        value=round(adjusted, -3),   # round to nearest £1000
        source="hpi_adjusted",
        source_label="HPI-Adjusted Estimate",
        is_estimate=True,
        notes=(
            f"Based on last sold ({base.notes}), adjusted for UK national "
            f"house price growth ({growth_pct:+.1f}% since sale)."
        ),
    )


# ── PropertyData.co.uk (UK, paid AVM) ─────────────────────────────────────────

def _property_data_uk(address: str, api_key: str) -> PropertyEstimate | None:
    """AVM estimate via propertydata.co.uk — requires paid API key."""
    match = _UK_POSTCODE_RE.search(address)
    if not match:
        log.debug("PropertyData: no postcode found in %r", address)
        return None

    postcode = match.group(1).upper().replace(" ", "")
    url = (
        f"https://api.propertydata.co.uk/valuation"
        f"?key={urllib.parse.quote(api_key)}"
        f"&postcode={urllib.parse.quote(postcode)}"
        f"&property_type=all"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = _json.loads(resp.read())
            # PropertyData returns {"status":"success","data":{"estimated_value":...}}
            price = (
                data.get("data", {}).get("estimated_value")
                or data.get("estimated_value")
            )
            if price:
                return PropertyEstimate(
                    value=float(price),
                    source="propertydata",
                    source_label="PropertyData AVM",
                    is_estimate=True,
                )
    except Exception as e:
        log.warning("PropertyData failed for %r: %s", address, e)
    return None


# ── Domain.com.au (AU) ─────────────────────────────────────────────────────────

def _domain_au(address: str, api_key: str) -> PropertyEstimate | None:
    """AVM estimate via Domain API — requires free developer key."""
    # First resolve address to a Domain property ID, then call AVM
    try:
        suggest_url = (
            "https://api.domain.com.au/v1/properties/_suggest"
            f"?terms={urllib.parse.quote(address)}&pageSize=1"
        )
        headers = {
            "Accept": "application/json",
            "X-Api-Key": api_key,
        }
        req = urllib.request.Request(suggest_url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            suggestions = _json.loads(resp.read())
            if not suggestions:
                return None
            prop_id = suggestions[0].get("id")
            if not prop_id:
                return None

        avm_url = f"https://api.domain.com.au/v2/properties/{prop_id}/priceEstimate"
        req = urllib.request.Request(avm_url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = _json.loads(resp.read())
            price = (
                data.get("midPrice")
                or data.get("estimate")
                or data.get("lowerPrice")
            )
            if price:
                return PropertyEstimate(
                    value=float(price),
                    source="domain_au",
                    source_label="Domain.com.au AVM",
                    is_estimate=True,
                )
    except Exception as e:
        log.warning("Domain API failed for %r: %s", address, e)
    return None
