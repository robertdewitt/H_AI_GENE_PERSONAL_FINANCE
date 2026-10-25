"""Single Jinja2 environment so every page gets build/version globals.

app_build_line and app_build_short are registered as callables so that
Jinja2 invokes them on each render — the dirty flag is therefore always
current without requiring a server restart after a commit.
"""

import json
from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.build_info import get_footer, get_short
from app.config import settings

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Lambdas so Jinja2 calls them fresh on every request.
templates.env.globals["app_build_line"] = lambda: get_footer(settings.app_version)
templates.env.globals["app_build_short"] = lambda: get_short(settings.app_version)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _tojson_decimal(value, **kwargs):
    """tojson filter that handles Decimal values."""
    return Markup(json.dumps(value, cls=_DecimalEncoder, **kwargs))


templates.env.filters["tojson"] = _tojson_decimal


# Currency-symbol filter — `{{ "GBP" | currency_symbol }}` → "£".
# Covers G10 + a handful of common others; unknown codes fall through to
# the code itself so the UI never silently shows a wrong symbol.
_CURRENCY_SYMBOLS = {
    "USD": "$",  "EUR": "€",  "GBP": "£",  "JPY": "¥",
    "CHF": "Fr", "CAD": "C$", "AUD": "A$", "NZD": "NZ$",
    "SEK": "kr", "NOK": "kr",
    "CNY": "¥",  "INR": "₹",  "BRL": "R$", "KRW": "₩",
    "MXN": "$",  "HKD": "HK$", "SGD": "S$",
}


def _currency_symbol(code: str | None) -> str:
    if not code:
        return ""
    return _CURRENCY_SYMBOLS.get(str(code).upper(), str(code))


templates.env.filters["currency_symbol"] = _currency_symbol
templates.env.globals["currency_symbol"] = _currency_symbol


# ── Mask a secret for display: "••••••wxyz" — shows the last few chars
# so the user knows it's set, without leaking the whole thing on screen.
from app.services.secret_box import mask as _mask_secret
templates.env.filters["mask_secret"] = _mask_secret


def _nav_accounts(request) -> dict[str, list]:
    """The signed-in user's open accounts for the nav menu, grouped and
    ordered as on the Accounts page: {group: [(id, name), ...]}.

    Reads the user and session get_current_user left on request.state, so
    it is empty on pages with no signed-in user (login, setup)."""
    state = getattr(request, "state", None)
    user, db = getattr(state, "user", None), getattr(state, "db", None)
    if user is None or db is None:
        return {}
    from app.services.scoping import owned_accounts

    groups: dict[str, list] = {}
    accounts = sorted(owned_accounts(db, user), key=lambda a: (a.account_type.value, a.name.lower()))
    for a in accounts:
        if a.closed_at is None:
            groups.setdefault(a.type_group, []).append((a.id, a.name))
    return groups


templates.env.globals["nav_accounts"] = _nav_accounts


_static_dir = Path(__file__).parent / "static"


def _static_url(path: str) -> str:
    """``/static/<path>?v=<mtime>`` — the query changes whenever the file
    does, so browsers never keep using a stale stylesheet after an edit."""
    try:
        version = int((_static_dir / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version}"


templates.env.globals["static_url"] = _static_url
