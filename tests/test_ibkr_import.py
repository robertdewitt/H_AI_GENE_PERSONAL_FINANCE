"""IBKR activity-statement parser smoke tests.

Constructs a minimal IBKR-shaped CSV inline (no committed binary fixture)
and asserts the parser pulls out base currency, period dates, and a
trades row with the right amount and quantity. Malformed input must
yield empty containers and not raise.
"""
from datetime import datetime
from pathlib import Path

import pytest

from app.services.ibkr_import import is_ibkr_file, parse_ibkr_csv


IBKR_HAPPY = (
    "Statement,Header,Field Name,Field Value\n"
    "Statement,Data,Name,Test Account\n"
    "Statement,Data,Base Currency,USD\n"
    "Statement,Data,Period,\"January 1, 2026 - January 31, 2026\"\n"
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
    "Trades,Data,Order,Stocks,USD,AAPL,\"2026-01-15, 10:00:00\",10,180.00,180.00,-1800.00,-1.00,1801.00,0,0,O\n"
    "Trades,Data,Order,Stocks,USD,MSFT,\"2026-01-20, 11:00:00\",-5,400.00,400.00,2000.00,-1.00,-1999.00,0,0,C\n"
    "Dividends,Header,Currency,Date,Description,Amount\n"
    "Dividends,Data,USD,2026-01-25,AAPL(US0378331005) Cash Dividend USD 0.24 per Share,2.40\n"
)


IBKR_MALFORMED_NO_STATEMENT_HEADER = (
    "RandomHeader,Some,Thing\n"
    "Data1,Data2,Data3\n"
)


def _write(tmp_path: Path, content: str, name: str = "ibkr.csv") -> str:
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_is_ibkr_file_recognises_well_formed_csv(tmp_path):
    p = _write(tmp_path, IBKR_HAPPY)
    assert is_ibkr_file(p) is True


def test_is_ibkr_file_rejects_random_csv(tmp_path):
    p = _write(tmp_path, IBKR_MALFORMED_NO_STATEMENT_HEADER)
    assert is_ibkr_file(p) is False


def test_parse_ibkr_csv_happy_path(tmp_path):
    p = _write(tmp_path, IBKR_HAPPY)
    parsed = parse_ibkr_csv(p)

    assert parsed["base_currency"] == "USD"
    assert isinstance(parsed["period_start"], datetime)
    assert parsed["period_start"].year == 2026 and parsed["period_start"].month == 1

    # Trades section: two rows, signed quantities + symbols
    trades = parsed["trades"]
    assert len(trades) == 2
    symbols = sorted(t["symbol"] for t in trades)
    assert symbols == ["AAPL", "MSFT"]
    aapl = next(t for t in trades if t["symbol"] == "AAPL")
    msft = next(t for t in trades if t["symbol"] == "MSFT")
    assert float(aapl["quantity"]) == pytest.approx(10.0)
    assert float(msft["quantity"]) == pytest.approx(-5.0)

    # Dividend row totals
    divs = parsed.get("dividends", [])
    assert len(divs) == 1
    assert float(divs[0]["amount"]) == pytest.approx(2.40)


def test_parse_ibkr_csv_malformed_returns_empty(tmp_path):
    """A CSV that isn't really IBKR must not throw; it should yield empty
    sections so the caller can flag it as unrecognised."""
    p = _write(tmp_path, IBKR_MALFORMED_NO_STATEMENT_HEADER)
    parsed = parse_ibkr_csv(p)
    assert parsed.get("trades", []) == []
    assert parsed.get("dividends", []) == []
