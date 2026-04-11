"""Parse structured multi-line financial documents from JSON/dict payloads.

Used for payroll payslips and rental property statements. Produces
validated dataclasses ready for persistence and split mapping.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class ParsedLine:
    kind: str
    label: str
    amount: float
    currency: str
    code: str | None = None
    is_cash: bool = True
    excluded_from_net_sum: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedFinancialDocument:
    document_type: str
    currency: str
    statement_date: datetime
    account_id_placeholder: int | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    reference: str | None = None
    employer_or_counterparty: str | None = None
    property_code: str | None = None
    net_pay: float | None = None
    net_bank_deposit: float | None = None
    lines: list[ParsedLine] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_dt(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()[:19]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt)
        except ValueError:
            continue
    return None


def _line_from_dict(d: dict[str, Any], default_currency: str) -> ParsedLine:
    return ParsedLine(
        kind=str(d.get("kind", "income")).lower(),
        label=str(d.get("label", d.get("code", ""))),
        amount=float(d.get("amount", 0)),
        currency=str(d.get("currency", default_currency)),
        code=d.get("code"),
        is_cash=bool(d.get("is_cash", True)),
        excluded_from_net_sum=bool(d.get("excluded_from_net_sum", False)),
        extra={k: v for k, v in d.items() if k not in {
            "kind", "label", "amount", "currency", "code",
            "is_cash", "excluded_from_net_sum",
        }},
    )


def parse_document_dict(data: dict[str, Any]) -> ParsedFinancialDocument:
    """Parse a document dict (e.g. from JSON). Raises ValueError on invalid."""
    doc_type = str(data.get("document_type", "")).lower()
    if doc_type not in ("payroll", "rental_statement"):
        raise ValueError(f"Unsupported document_type: {doc_type}")

    currency = str(data.get("currency", "USD"))
    stmt = _parse_dt(data.get("statement_date") or data.get("pay_date"))
    if stmt is None:
        raise ValueError("statement_date or pay_date is required")

    period_start = _parse_dt(data.get("period_start") or (data.get("pay_period") or {}).get("start"))
    period_end = _parse_dt(data.get("period_end") or (data.get("pay_period") or {}).get("end"))
    if period_start is None and "period" in data:
        period_start = _parse_dt(data["period"].get("start"))
        period_end = _parse_dt(data["period"].get("end"))

    lines_raw = data.get("lines") or []
    if not isinstance(lines_raw, list):
        raise ValueError("lines must be a list")
    lines = [_line_from_dict(x, currency) for x in lines_raw]

    net_pay = data.get("net_pay")
    net_bank = data.get("net_bank_deposit")

    if doc_type == "payroll":
        if net_pay is None:
            raise ValueError("payroll document requires net_pay")
        net_pay = float(net_pay)
        _validate_payroll_sum(lines, net_pay)
    else:
        if net_bank is None:
            raise ValueError("rental_statement requires net_bank_deposit")
        net_bank = float(net_bank)
        _validate_rental_sum(lines, net_bank)

    return ParsedFinancialDocument(
        document_type=doc_type,
        currency=currency,
        statement_date=stmt,
        period_start=period_start,
        period_end=period_end,
        reference=data.get("reference"),
        employer_or_counterparty=data.get("employer") or data.get("counterparty"),
        property_code=data.get("property_code"),
        net_pay=float(net_pay) if net_pay is not None else None,
        net_bank_deposit=float(net_bank) if net_bank is not None else None,
        lines=lines,
        raw=dict(data),
    )


def _validate_payroll_sum(lines: list[ParsedLine], net_pay: float, tol: float = 0.02) -> None:
    included = [ln for ln in lines if not ln.excluded_from_net_sum]
    s = sum(ln.amount for ln in included)
    if abs(s - net_pay) > tol:
        raise ValueError(
            f"Payroll lines sum {s:.2f} does not match net_pay {net_pay:.2f}",
        )


def _validate_rental_sum(lines: list[ParsedLine], net_bank: float, tol: float = 0.02) -> None:
    included = [ln for ln in lines if not ln.excluded_from_net_sum]
    s = sum(ln.amount for ln in included)
    if abs(s - net_bank) > tol:
        raise ValueError(
            f"Rental lines sum {s:.2f} does not match net_bank_deposit {net_bank:.2f}",
        )


def parse_document_json(path: str | Path) -> ParsedFinancialDocument:
    """Load and parse a JSON file."""
    p = Path(path)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return parse_document_dict(data)
