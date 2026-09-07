"""Documents that arrive with no text layer.

WTW's ePA portal issues no statement — what gets uploaded is a screenshot of
the balances page saved as a PDF: one page, one image, zero characters. Every
text parser in this app failed on it identically and silently, so the upload
appeared to do nothing at all.
"""
from decimal import Decimal

import pytest

from app.services import vision_extract as vx


@pytest.fixture(autouse=True)
def _no_model_by_default(monkeypatch):
    monkeypatch.setattr(vx, "vision_model_available", lambda: False)


def _payload(**overrides):
    base = {
        "total_value": "373365.28",
        "currency": "GBP",
        "funds": [
            {"name": "North American Equity-Passive", "units": "15441.51",
             "unit_price": "12.53", "price_date": "03/09/2026",
             "value": "193482.11"},
            {"name": "Global Equity - Active", "units": "15393.49",
             "unit_price": "7.479", "price_date": "03/09/2026",
             "value": "115127.90"},
            {"name": "World (ex-UK) Equity - Passive", "units": "6562.81",
             "unit_price": "9.867", "price_date": "03/09/2026",
             "value": "64755.27"},
        ],
    }
    base.update(overrides)
    return base


def _with_model(monkeypatch, payload):
    monkeypatch.setattr(vx, "vision_model_available", lambda: True)
    monkeypatch.setattr(vx, "page_images", lambda *a, **k: ["<png>"])
    monkeypatch.setattr(vx, "_ask_vision", lambda images: payload)


# ── Nothing installed ────────────────────────────────────────────────────


def test_returns_none_without_a_vision_model():
    assert vx.extract_pension_balances("anything.pdf") is None


def test_a_failed_read_returns_none(monkeypatch):
    monkeypatch.setattr(vx, "vision_model_available", lambda: True)
    monkeypatch.setattr(vx, "page_images", lambda *a, **k: ["<png>"])
    monkeypatch.setattr(vx, "_ask_vision", lambda images: None)

    assert vx.extract_pension_balances("x.pdf") is None


# ── Reading the page ─────────────────────────────────────────────────────


def test_extracts_every_fund(monkeypatch):
    _with_model(monkeypatch, _payload())

    parsed = vx.extract_pension_balances("x.pdf")

    assert parsed is not None and len(parsed.funds) == 3
    assert parsed.total_value == Decimal("373365.28")
    assert parsed.currency == "GBP"
    first = parsed.funds[0]
    assert first.units == Decimal("15441.51")
    assert first.unit_price == Decimal("12.53")
    assert first.value == Decimal("193482.11")
    assert first.price_date.isoformat() == "2026-09-03"


def test_thousands_separators_and_currency_symbols_are_stripped(monkeypatch):
    _with_model(monkeypatch, _payload(funds=[
        {"name": "Fund", "units": "15,441.51", "unit_price": "12.53",
         "price_date": "03/09/2026", "value": "£193,482.11"},
    ], total_value="£193,482.11"))

    parsed = vx.extract_pension_balances("x.pdf")

    assert parsed.funds[0].units == Decimal("15441.51")
    assert parsed.funds[0].value == Decimal("193482.11")


# ── The model does not get the last word on the numbers ──────────────────


def test_a_row_that_does_not_reconcile_is_rejected(monkeypatch):
    """units x unit price must match the stated fund value — this is what
    catches a misread digit."""
    bad = _payload()
    bad["funds"][0]["units"] = "1544.15"        # a decimal point moved
    _with_model(monkeypatch, bad)

    assert vx.extract_pension_balances("x.pdf") is None


def test_funds_that_do_not_sum_to_the_total_are_rejected(monkeypatch):
    _with_model(monkeypatch, _payload(total_value="999999.99"))

    assert vx.extract_pension_balances("x.pdf") is None


def test_a_row_missing_a_number_is_skipped_not_invented(monkeypatch):
    partial = _payload(funds=[
        {"name": "Fund A", "units": "100.00", "unit_price": "2.00",
         "price_date": "03/09/2026", "value": "200.00"},
        {"name": "Fund B", "units": None, "unit_price": "3.00",
         "price_date": "03/09/2026", "value": "300.00"},
    ], total_value="200.00")
    _with_model(monkeypatch, partial)

    parsed = vx.extract_pension_balances("x.pdf")

    assert parsed is not None
    assert [f.name for f in parsed.funds] == ["Fund A"]


def test_no_funds_at_all_returns_none(monkeypatch):
    _with_model(monkeypatch, _payload(funds=[]))

    assert vx.extract_pension_balances("x.pdf") is None


# ── Text-layer detection ─────────────────────────────────────────────────


def test_the_real_screenshot_has_no_text_layer():
    """The actual file that failed."""
    path = (
        "uploads/1/20260906T220740Z_29b7d09d_image001 (3).png.pdf"
    )
    import os
    if not os.path.exists(path):
        pytest.skip("sample upload not present")

    assert vx.has_text_layer(path) is False


def test_probe_fails_open_on_an_unreadable_file():
    """If the probe itself breaks, let the normal parsers try rather than
    blocking the upload."""
    assert vx.has_text_layer("/nonexistent/nope.pdf") is True


# ── Transcription parsing ────────────────────────────────────────────────


def test_parses_the_delimited_transcription():
    """What the model actually returns, verbatim from a real run."""
    text = (
        "North American Equity-Passive | 15441.51 | 12.53 | 03/09/2026 | 193482.11\n"
        "Global Equity - Active | 15393.49 | 7.479 | 03/09/2026 | 115127.90\n"
        "World (ex-UK) Equity - Passive | 6562.81 | 9.867 | 03/09/2026 | 64755.27\n"
        "TOTAL | 373365.28"
    )

    payload = vx._parse_delimited(text)

    assert payload["total_value"] == "373365.28"
    assert len(payload["funds"]) == 3
    assert payload["funds"][0] == {
        "name": "North American Equity-Passive", "units": "15441.51",
        "unit_price": "12.53", "price_date": "03/09/2026", "value": "193482.11",
    }


def test_transcription_ignores_commentary_and_headers():
    text = (
        "Here is the table you asked for:\n"
        "Fund | Units | Unit Price | Unit Price Date | Fund Value\n"
        "Real Fund | 100.00 | 2.00 | 01/02/2026 | 200.00\n"
        "short | row\n"
        "TOTAL | 200.00\n"
    )

    payload = vx._parse_delimited(text)

    assert [f["name"] for f in payload["funds"]] == ["Real Fund"]
    assert payload["total_value"] == "200.00"


def test_empty_transcription_yields_no_funds():
    assert vx._parse_delimited("")["funds"] == []
    assert vx._parse_delimited("I cannot read this image.")["funds"] == []
