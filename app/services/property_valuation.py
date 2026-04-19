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


def estimate_property_value(
    address: str,
    currency: str,
    country_of_residence: str | None = None,
    rentcast_api_key: str | None = None,
    property_data_api_key: str | None = None,
    domain_api_key: str | None = None,
) -> PropertyEstimate | None:
    """Route to the right provider and return an estimate, or None."""
    region = _detect_region(currency, country_of_residence)
    log.info("Property lookup: region=%s address=%r", region, address)

    if region == "US":
        result = _rentcast(address, rentcast_api_key)
        if result:
            return result

    elif region == "UK":
        if property_data_api_key:
            result = _property_data_uk(address, property_data_api_key)
            if result:
                return result
        # Free fallback: HM Land Registry last-sold price
        result = _hm_land_registry(address)
        if result:
            return result

    elif region == "AU":
        if domain_api_key:
            result = _domain_au(address, domain_api_key)
            if result:
                return result

    return None


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


def _hm_land_registry(address: str) -> PropertyEstimate | None:
    """Look up the most recent transaction price for a UK postcode via
    the HM Land Registry Price Paid Data API (free, no key required).
    """
    match = _UK_POSTCODE_RE.search(address)
    if not match:
        log.debug("HM Land Registry: no postcode found in %r", address)
        return None

    postcode = match.group(1).upper().strip()
    # Use + encoding for postcode spaces (required by this endpoint)
    encoded_postcode = urllib.parse.quote(postcode, safe="")
    url = (
        "https://landregistry.data.gov.uk/data/ppi/transaction-record.json"
        f"?propertyAddress.postcode={encoded_postcode}&_pageSize=5"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = _json.loads(resp.read())
            items = data.get("result", {}).get("items", [])
            if not items:
                log.warning("HM Land Registry: no results for postcode %s", postcode)
                return None
            # Sort by transactionDate descending to get most recent
            def _parse_date(item):
                try:
                    import email.utils
                    return email.utils.parsedate(item.get("transactionDate", "")) or (0,)
                except Exception:
                    return (0,)
            items_sorted = sorted(items, key=_parse_date, reverse=True)
            latest = items_sorted[0]
            price = latest.get("pricePaid")
            if price:
                date_str = latest.get("transactionDate", "unknown date")
                return PropertyEstimate(
                    value=float(price),
                    source="hm_land_registry",
                    source_label="HM Land Registry (last sold)",
                    is_estimate=False,
                    notes=f"Last sold: {date_str}",
                )
    except Exception as e:
        log.warning("HM Land Registry failed for postcode %s: %s", postcode, e)
    return None


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
