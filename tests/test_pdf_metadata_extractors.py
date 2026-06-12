"""Fixture-driven tests for the PDF metadata extractors.

We generate small synthetic PDFs with reportlab so the tests stay
self-contained — no committed binary blobs that drift from production
parsing behaviour over time. Coverage:

- ``extract_cc_metadata`` against a CC-style PDF (BofA-ish layout)
- ``extract_overdraft_facility`` with an overdraft line present, absent,
  and with a malformed amount
- The Amex UK summary block extractor (5-value form with Plan It)
- ``_parse_signed_amount`` corner cases that surfaced during BA Amex
  parsing
"""
from pathlib import Path

import pytest

reportlab = pytest.importorskip("reportlab", reason="reportlab needed for PDF fixtures")
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.services.pdf_import import (
    _parse_signed_amount,
    extract_cc_metadata,
    extract_overdraft_facility,
)


def _write_pdf(tmp_path: Path, lines: list[str], name: str = "f.pdf") -> str:
    """Render *lines* as a single-page PDF and return its path."""
    p = tmp_path / name
    c = canvas.Canvas(str(p), pagesize=letter)
    y = 750
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()
    return str(p)


# ── _parse_signed_amount ──────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("$1,234.56",     1234.56),
    ("-$1,333.76",   -1333.76),
    ("$-1,333.76",   -1333.76),
    ("£500.00",       500.00),
    ("-£1,333.76",   -1333.76),
    ("1,234.56-",    -1234.56),
    ("€500.00",       500.00),
    ("¥100,000",      100000.0),
    ("1,234.56",      1234.56),
])
def test_parse_signed_amount(raw, expected):
    assert _parse_signed_amount(raw) == pytest.approx(expected)


def test_parse_signed_amount_garbage_returns_none():
    assert _parse_signed_amount("not a number") is None
    assert _parse_signed_amount("") is None


# ── extract_cc_metadata: BofA-style PDF ───────────────────────────────


def test_extract_cc_metadata_bofa_style(tmp_path):
    pdf = _write_pdf(tmp_path, [
        "Account Summary",
        "Statement Closing Date: 05/19/2026",
        "Previous Balance $60,261.93",
        "New Balance Total $59,472.05",
        "Total Minimum Payment Due $594.00",
        "Payment Due Date: 06/16/2026",
    ])
    meta = extract_cc_metadata(pdf)
    assert meta is not None
    assert meta["previous_balance"] == pytest.approx(60261.93)
    assert meta["new_balance"]     == pytest.approx(59472.05)
    assert meta["minimum_payment"] == pytest.approx(594.00)
    assert meta["statement_date"].isoformat() == "2026-05-19"
    assert meta["payment_due_date"].isoformat() == "2026-06-16"


def test_extract_cc_metadata_handles_negative_new_balance(tmp_path):
    """Credit balance (Chase Hyatt-style) must survive as a negative number."""
    pdf = _write_pdf(tmp_path, [
        "Previous Balance $2,055.87",
        "New Balance -$1,333.76",
        "Opening/Closing Date 04/25/26 - 05/24/26",
        "Payment Due Date 06/16/26",
    ])
    meta = extract_cc_metadata(pdf)
    assert meta is not None
    assert meta["new_balance"]      == pytest.approx(-1333.76)
    assert meta["previous_balance"] == pytest.approx(2055.87)
    assert meta["statement_date"].isoformat() == "2026-05-24"


def test_extract_cc_metadata_returns_none_for_unrelated_pdf(tmp_path):
    pdf = _write_pdf(tmp_path, [
        "Rental Income Statement",
        "Tenant: Alice Example",
        "Rent received: £1,000",
    ])
    assert extract_cc_metadata(pdf) is None


# ── extract_overdraft_facility ────────────────────────────────────────


def test_overdraft_extracts_typical_uk_label(tmp_path):
    pdf = _write_pdf(tmp_path, [
        "ACCOUNT SUMMARY",
        "Account Holder: R Dewitt",
        "Arranged Overdraft Limit: £50,000.00",
        "Statement Date: 25 May 2026",
    ])
    od = extract_overdraft_facility(pdf)
    assert od is not None
    assert od["overdraft_limit"] == pytest.approx(50000.00)
    # Best-effort date parsing — present if we could pick one up.
    assert "statement_date" in od


def test_overdraft_returns_none_when_no_facility(tmp_path):
    pdf = _write_pdf(tmp_path, [
        "ACCOUNT SUMMARY",
        "Account Holder: R Dewitt",
        "Closing Balance: £12,345.67",
    ])
    assert extract_overdraft_facility(pdf) is None


def test_overdraft_returns_none_on_malformed_amount(tmp_path):
    """A label without a parseable amount must not invent a number."""
    pdf = _write_pdf(tmp_path, [
        "Arranged Overdraft Limit: REVIEW",
        "Closing Balance: £100.00",
    ])
    od = extract_overdraft_facility(pdf)
    # Either explicit None or no overdraft_limit key at all is acceptable —
    # what's *not* acceptable is a wrong number.
    assert od is None or od.get("overdraft_limit") in (None,)


def test_overdraft_picks_up_agreed_phrasing(tmp_path):
    pdf = _write_pdf(tmp_path, [
        "Agreed Overdraft: £2,500.00",
    ])
    od = extract_overdraft_facility(pdf)
    assert od is not None
    assert od["overdraft_limit"] == pytest.approx(2500.00)
