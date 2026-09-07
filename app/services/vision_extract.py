"""Read documents that carry no text layer.

Some providers never issue a statement. WTW's ePA portal, for instance, only
shows a balances page on screen — so what arrives here is a screenshot saved
as a PDF: one page, one image, zero characters. Every text parser in this app
fails on that identically and silently, which is how an upload could appear to
"do nothing".

Two things live here. ``has_text_layer`` is deterministic and always
available: it tells the upload route to say so plainly instead of importing
nothing. ``extract_pension_balances`` uses a local multimodal model when one
is installed, and returns None when it isn't.

The model reads the *page*; it never gets the last word on the numbers. What
it returns goes to the existing preview screen for a human to confirm before
anything is written, and the arithmetic is checked first — units × unit price
has to reconcile to the stated fund value, and the funds have to sum to the
stated total. A model that misreads a digit fails that check.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

from app.config import settings

if TYPE_CHECKING:
    from app.services.epa_pension_import import ParsedPension

log = logging.getLogger(__name__)

# Tolerance when checking the model's numbers against each other, in pounds.
RECONCILE_TOLERANCE = Decimal("1.00")


def has_text_layer(filepath: str, min_chars: int = 40) -> bool:
    """Whether the PDF carries extractable text at all."""
    try:
        import pdfplumber

        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages[:3]:
                if len((page.extract_text() or "").strip()) >= min_chars:
                    return True
    except Exception as exc:
        log.debug("text-layer probe failed for %s: %s", filepath, exc)
        return True      # fail open: let the normal parsers try
    return False


def page_images(filepath: str, max_pages: int = 3, resolution: int = 200) -> list[str]:
    """Render pages to base64 PNGs for a vision model."""
    out: list[str] = []
    try:
        import pdfplumber

        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages[:max_pages]:
                buf = io.BytesIO()
                page.to_image(resolution=resolution).save(buf, format="PNG")
                out.append(base64.b64encode(buf.getvalue()).decode())
    except Exception as exc:
        log.warning("could not render %s for vision: %s", filepath, exc)
    return out


def resolve_vision_model() -> str | None:
    """The installed tag to call, or None if nothing matches.

    The setting may name a family ("gemma4") while what is actually pulled
    carries a size and quantisation ("gemma4:26b-a4b-it-q8_0"). Ollama will
    not resolve the bare family name to that tag, so match it here and call
    the tag that exists — otherwise pulling a specific size silently leaves
    extraction switched off.
    """
    try:
        resp = httpx.get(f"{settings.ollama_url}/api/tags", timeout=2.0)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        return None

    wanted = settings.ollama_vision_model
    if wanted in names:
        return wanted
    base = wanted.split(":")[0]
    matches = sorted(n for n in names if n.split(":")[0] == base)
    return matches[0] if matches else None


def vision_model_available() -> bool:
    """Whether a multimodal model is installed locally."""
    return resolve_vision_model() is not None


_PROMPT = """Transcribe the "Balance By Fund" table from this image.

Output one line per fund, using | as the separator, in this exact order:
fund name | units | unit price | unit price date | fund value

Then a final line: TOTAL | total value

Copy the numbers exactly as printed but remove thousands separators and
currency symbols. Output nothing else — no headings, no commentary."""


def _dec(raw) -> Decimal | None:
    if raw is None:
        return None
    text = re.sub(r"[^\d.\-]", "", str(raw))
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_delimited(text: str) -> dict:
    """Turn the model's pipe-delimited transcription into the payload shape.

    Deliberately not JSON. Asked for JSON the model returns the right numbers
    under invented keys ("unit_counts", "unit_density") and stray array
    elements; asked to transcribe a table it is exact and reproducible. So it
    reads, and this parses — the split every extraction here is built on.
    """
    funds: list[dict] = []
    total = None
    for line in (text or "").splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        if cells[0].upper().startswith("TOTAL"):
            total = cells[1] if len(cells) > 1 else None
            continue
        if len(cells) < 5:
            continue
        name, units, price, price_date, value = cells[:5]
        if not name or name.lower() == "fund":      # skip a header row
            continue
        funds.append({
            "name": name, "units": units, "unit_price": price,
            "price_date": price_date, "value": value,
        })
    return {"total_value": total, "currency": "GBP", "funds": funds}


def _ask_vision(images: list[str]) -> dict | None:
    model = resolve_vision_model()
    if model is None:
        return None
    try:
        resp = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": _PROMPT,
                "images": images,
                "stream": False,
                # Greedy decoding: the same page must transcribe the same way
                # every time, or re-importing a file changes the ledger.
                "options": {"temperature": 0},
            },
            timeout=settings.ollama_vision_timeout,
        )
        resp.raise_for_status()
        return _parse_delimited(resp.json().get("response", ""))
    except Exception as exc:
        log.warning("vision extraction failed: %s", exc)
        return None


def extract_pension_balances(filepath: str) -> "ParsedPension | None":
    """Read a fund-balances page that has no text layer.

    Returns None when no vision model is installed, when the page cannot be
    read, or when the numbers do not reconcile against each other.
    """
    from app.services.epa_pension_import import ParsedPension, PensionFund

    if not vision_model_available():
        return None
    images = page_images(filepath)
    if not images:
        return None
    payload = _ask_vision(images)
    if not isinstance(payload, dict):
        return None

    funds: list[PensionFund] = []
    for raw in payload.get("funds") or []:
        if not isinstance(raw, dict):
            continue
        units = _dec(raw.get("units"))
        price = _dec(raw.get("unit_price"))
        value = _dec(raw.get("value"))
        name = (raw.get("name") or "").strip()
        if not name or units is None or price is None or value is None:
            continue
        # units × price must reconcile to the stated value; a misread digit
        # in any of the three breaks this and the row is rejected.
        if abs(units * price - value) > RECONCILE_TOLERANCE:
            log.warning(
                "vision row rejected — %s: %s x %s != %s", name, units, price, value,
            )
            return None
        price_date = None
        raw_date = (raw.get("price_date") or "").strip()
        if raw_date:
            from datetime import datetime
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
                try:
                    price_date = datetime.strptime(raw_date, fmt).date()
                    break
                except ValueError:
                    continue
        funds.append(PensionFund(
            name=name, units=units, unit_price=price,
            price_date=price_date, value=value,
        ))

    if not funds:
        return None

    total = _dec(payload.get("total_value"))
    if total is not None and abs(sum(f.value for f in funds) - total) > RECONCILE_TOLERANCE:
        log.warning("vision totals do not reconcile: rows sum != stated total")
        return None

    return ParsedPension(
        total_value=total, currency=payload.get("currency") or "GBP", funds=funds,
    )
