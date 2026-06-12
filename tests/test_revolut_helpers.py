"""Unit tests for the Revolut PDF parser helpers.

The full text extraction is layout-coordinate driven and not worth
synthesising end-to-end; instead we cover the helpers that decide
*how to interpret* each row — these were the source of past parsing
bugs (sign convention, ambiguous section headers).
"""
from decimal import Decimal

from app.services.revolut_pdf_parser import (
    _identify_section,
    _parse_amount,
    _parse_date,
)


def test_parse_amount_negative_pound():
    assert _parse_amount("-£1,234.56") == Decimal("-1234.56")


def test_parse_amount_positive_no_symbol():
    assert _parse_amount("12.34") == Decimal("12.34")


def test_parse_amount_handles_thousands():
    assert _parse_amount("£1,000,000.00") == Decimal("1000000.00")


def test_parse_amount_invalid_returns_none():
    assert _parse_amount("not a number") is None
    assert _parse_amount("") is None


def test_parse_date_three_token_dd_mon_yyyy():
    d = _parse_date(["27", "Mar", "2026"])
    assert d is not None and d.year == 2026 and d.month == 3 and d.day == 27


def test_parse_date_rejects_unknown_month():
    assert _parse_date(["27", "Mrz", "2026"]) is None


def test_identify_section_main_account():
    out = _identify_section("Account transactions from 27 March 2022 to 25 May 2026")
    assert out is not None
    key, label, is_main = out
    assert key == "main" and is_main is True


def test_identify_section_named_account():
    out = _identify_section("Maya's account transactions from 27 March 2022")
    assert out is not None
    key, label, is_main = out
    assert key == "maya" and is_main is False
    assert "Maya" in label


def test_identify_section_pockets_and_savings():
    p = _identify_section("Personal and Group Pockets transactions")
    s = _identify_section("Savings transactions from 1 Jan 2026")
    assert p is not None and p[0] == "pockets"
    assert s is not None and s[0] == "savings"


def test_identify_section_returns_none_for_unrelated_row():
    assert _identify_section("Some unrelated marketing copy") is None
