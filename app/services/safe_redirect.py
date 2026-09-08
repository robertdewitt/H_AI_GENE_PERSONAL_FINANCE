"""One validator for every redirect built from request input.

Forms carry a ``return_url`` / ``return_to`` so a POST can send the user back
where they came from. Anything a browser follows verbatim is an open redirect
if it can name another origin — a phishing link on this app's own domain
that lands on an attacker's copy of the login page.

Several routes had no check at all, and the ones that did checked only
``startswith("//")``. That misses two things browsers do before following a
Location header:

* A backslash is normalised to a slash, so ``/\\evil.example`` becomes
  ``//evil.example`` — a protocol-relative URL to another host.
* A carriage return or newline splits the header, so a value containing one
  can inject further headers or a body.

Accept only what a same-origin path can be: starts with a single ``/``, no
backslash anywhere, no control characters. Everything else goes to the
caller's fallback, which is always a hard-coded path.
"""
from __future__ import annotations


def safe_return_to(value: object, fallback: str) -> str:
    """Return ``value`` if it is a same-origin path, else ``fallback``.

    Tests call routes as plain functions, where an unfilled ``Form()`` default
    arrives instead of a string — that is "no destination given", not an
    error, so anything non-str yields the fallback.
    """
    if not isinstance(value, str):
        return fallback
    dest = value.strip()
    if not dest.startswith("/"):
        return fallback
    # "//host" is protocol-relative; "/\host" becomes it after the browser
    # normalises the backslash.
    if len(dest) > 1 and dest[1] in ("/", "\\"):
        return fallback
    if "\\" in dest:
        return fallback
    # CR/LF split the Location header; other controls are never legitimate.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in dest):
        return fallback
    return dest
