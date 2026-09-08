"""Redirects built from request input must never leave the site.

The previous checks looked only for a leading "//". Browsers normalise a
backslash to a slash before following a Location header, so "/\\evil" is
"//evil" by the time it is followed — and a CR/LF in the value splits the
header. Both got through.
"""
import pytest

from app.services.safe_redirect import safe_return_to

FALLBACK = "/fallback"


@pytest.mark.parametrize("hostile", [
    "https://evil.example/steal",
    "http://evil.example",
    "//evil.example",
    "///evil.example",
    "/\\evil.example",                 # backslash → slash → protocol-relative
    "/\\\\evil.example",
    "/\\/evil.example",
    "\\\\evil.example",
    "/accounts/8\r\nSet-Cookie: session=x",   # header injection
    "/accounts/8\nX: y",
    "/accounts/8\x00",
    "javascript:alert(1)",
    "evil.example",
    "accounts/8",                      # relative, no leading slash
    "",
    "   ",
    " //evil.example",
])
def test_hostile_values_go_to_the_fallback(hostile):
    assert safe_return_to(hostile, FALLBACK) == FALLBACK


@pytest.mark.parametrize("good", [
    "/",
    "/accounts/8",
    "/accounts/8?forecast_months=6",
    "/accounts/8?forecast_months=6#forecast",
    "/transactions?account_id=8&search=tesco",
    "/scheduled/review",
    "/%2f%2fevil.example",   # stays a path — %2f is not decoded into an authority
])
def test_same_origin_paths_pass_through(good):
    assert safe_return_to(good, FALLBACK) == good


def test_surrounding_whitespace_is_trimmed_not_rejected():
    assert safe_return_to("  /accounts/8  ", FALLBACK) == "/accounts/8"


def test_non_strings_yield_the_fallback():
    """Routes are called as plain functions in tests, where an unfilled
    Form() default arrives instead of a string."""
    class Sentinel:
        pass

    assert safe_return_to(None, FALLBACK) == FALLBACK
    assert safe_return_to(Sentinel(), FALLBACK) == FALLBACK
    assert safe_return_to(123, FALLBACK) == FALLBACK


def test_the_fallback_is_returned_verbatim():
    assert safe_return_to("//evil", "/accounts/42") == "/accounts/42"
