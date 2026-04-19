"""PDF bank statement extraction.

Strategy (in order):
  1. Extract tables via pdfplumber — works for PDFs with embedded table structure.
  2. Bank-statement text parser — for column-aligned text statements (Chase, Amex,
     Barclays, etc.):  each transaction line is  DATE  DESCRIPTION  AMOUNT
     where DATE may be MM/DD (no year), DD/MM/YYYY, YYYY-MM-DD, etc.

Returns a pandas DataFrame ready for detect_columns → import_transactions.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)


# ── Public entry point ────────────────────────────────────────────────────────

def pdf_to_dataframe(filepath: str) -> pd.DataFrame:
    """Extract transactions from a PDF bank statement → DataFrame."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError(
            "pdfplumber is required for PDF import. Run: pip install pdfplumber"
        )

    with pdfplumber.open(filepath) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        # Attempt 1: proper table extraction
        df = _extract_tables(pdf)
        if df is not None and len(df) >= 2 and _looks_like_transactions(df):
            log.info("PDF table extraction: %d rows, %d cols", len(df), len(df.columns))
            return df

    # Attempt 2: text-line parsing (handles most bank statement formats)
    df = _parse_statement_text(full_text)
    log.info("PDF text-line extraction: %d rows", len(df))
    return df


# ── Table extraction ──────────────────────────────────────────────────────────

def _extract_tables(pdf) -> pd.DataFrame | None:
    all_tables: list[list[list[Any]]] = []
    for page in pdf.pages:
        tables = page.extract_tables()
        if tables:
            all_tables.extend(tables)
    if not all_tables:
        return None

    by_ncols: dict[int, list[list[Any]]] = {}
    for tbl in all_tables:
        if not tbl:
            continue
        ncols = max(len(row) for row in tbl)
        if ncols >= 2:
            by_ncols.setdefault(ncols, []).extend(tbl)

    if not by_ncols:
        return None

    best_ncols = max(by_ncols, key=lambda k: len(by_ncols[k]))
    rows = by_ncols[best_ncols]

    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    looks_like_header = sum(
        1 for h in header
        if h and not re.match(r"^[-+$£€\d,.\s]+$", h)
    ) >= 2

    data_rows = rows[1:] if looks_like_header else rows
    if not looks_like_header:
        header = [f"col_{i}" for i in range(best_ncols)]

    norm: list[list[str]] = []
    for row in data_rows:
        cells = [str(c).strip() if c is not None else "" for c in row]
        while len(cells) < len(header):
            cells.append("")
        norm.append(cells[: len(header)])

    df = pd.DataFrame(norm, columns=header)
    df = df[df.apply(lambda r: any(v.strip() for v in r), axis=1)]
    return df.reset_index(drop=True) if len(df) > 0 else None


def _looks_like_transactions(df: pd.DataFrame) -> bool:
    """Sanity-check: does the DataFrame have at least a date-like and amount-like column?"""
    for col in df.columns:
        vals = df[col].dropna().astype(str)
        if vals.str.match(r"\d{1,4}[/\-\.]\d{1,2}").sum() > len(df) * 0.3:
            return True
    return False


# ── Text-line statement parser ────────────────────────────────────────────────

# Matches end-of-line amount:  optional sign, optional $£€, digits, optional cents
_EOL_AMOUNT_RE = re.compile(
    r"([-−–+]?\s*[$£€¥]?\s?\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?)\s*$"
)

# Full-line date patterns (ordered most-specific first)
_DATE_PATTERNS: list[tuple[re.Pattern, str, bool]] = [
    # YYYY-MM-DD
    (re.compile(r"^(\d{4}[-/]\d{2}[-/]\d{2})\b"), "%Y-%m-%d", False),
    # DD/MM/YYYY or DD-MM-YYYY
    (re.compile(r"^(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})\b"), None, True),
    # MM/DD (no year — Chase / Amex US)
    (re.compile(r"^(\d{2}/\d{2})\b"), "MM/DD", False),
    # DD Mon YYYY or Mon DD, YYYY
    (re.compile(r"^(\d{1,2}\s+[A-Za-z]{3}\w*\s+\d{2,4})\b"), None, True),
    (re.compile(r"^([A-Za-z]{3}\w*\s+\d{1,2}[,\s]+\d{4})\b"), None, False),
]

# Lines that are definitely NOT transactions
_SKIP_RE = re.compile(
    r"^("
    r"page\s+\d|statement\s+date|account\s+number|opening|closing|"
    r"minimum\s+payment|credit\s+limit|available|previous\s+balance|"
    r"new\s+balance|total\s+fees|total\s+interest|year.to.date|"
    r"annual\s+percentage|balance\s+type|subtotal|total"
    r")",
    re.IGNORECASE,
)

# Section headers that tell us what sign to expect next (Chase / Amex style)
_CREDIT_SECTION_RE = re.compile(
    r"^(PAYMENTS?\s+AND\s+OTHER\s+CREDITS?|CREDITS?|RETURNS?|REFUNDS?)\s*$",
    re.IGNORECASE,
)
_DEBIT_SECTION_RE = re.compile(
    r"^(PURCHASES?|TRANSACTIONS?|DEBITS?|FEES?\s+CHARGED|CASH\s+ADVANCES?|"
    r"BALANCE\s+TRANSFERS?)\s*$",
    re.IGNORECASE,
)

# FX continuation line: "103.13 X 1.3578 (EXCHG RATE)"
_FX_LINE_RE = re.compile(r"^\d[\d,\.]+\s+X\s+[\d\.]+\s*\(", re.IGNORECASE)


def _infer_year(text: str) -> int:
    """Try to extract a statement year from the PDF text."""
    # Look for 4-digit years between 2000-2099
    years = re.findall(r"\b(20\d{2})\b", text)
    if years:
        from collections import Counter
        return int(Counter(years).most_common(1)[0][0])
    from datetime import datetime
    return datetime.now().year


def _parse_date_mm_dd(raw: str, year: int) -> str | None:
    """Convert MM/DD string to YYYY-MM-DD using inferred year."""
    try:
        m, d = raw.split("/")
        return f"{year}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return None


def _normalise_amount(raw: str) -> str:
    """Normalise an amount string: strip symbols, handle unicode minus."""
    s = raw.strip()
    s = s.replace("−", "-").replace("–", "-")  # unicode minus/en-dash
    s = re.sub(r"[$£€¥\s]", "", s)
    s = s.replace(",", "")
    return s


def _deduplicate_chars(text: str) -> str:
    """Fix Chase's doubled-character encoding artifact: 'MMaannaaggee' → 'Manage'."""
    # Only apply to lines where every char appears exactly twice consecutively
    result = []
    for line in text.splitlines():
        # Check if line looks doubled: even length, each char repeated
        if len(line) >= 4 and len(line) % 2 == 0:
            halved = "".join(line[i] for i in range(0, len(line), 2))
            doubled_back = "".join(c * 2 for c in halved)
            if doubled_back == line:
                result.append(halved)
                continue
        result.append(line)
    return "\n".join(result)


def _parse_statement_text(text: str) -> pd.DataFrame:
    """Parse bank statement text into a DataFrame of transactions."""
    text = _deduplicate_chars(text)
    year = _infer_year(text)
    rows: list[dict] = []

    # Track section context to handle sign-less amounts (Chase/Amex style)
    in_credit_section = False

    prev_date: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Skip clearly non-transaction lines
        if _SKIP_RE.match(line):
            continue

        # FX continuation lines — attach exchange rate info to last transaction
        if _FX_LINE_RE.match(line) and rows:
            rows[-1]["Notes"] = line
            continue

        # Section header detection
        if _CREDIT_SECTION_RE.match(line):
            in_credit_section = True
            continue
        if _DEBIT_SECTION_RE.match(line):
            in_credit_section = False
            continue

        # Try to match a date at the start of the line
        date_str: str | None = None
        rest: str = line

        for pat, fmt, _dayfirst in _DATE_PATTERNS:
            m = pat.match(line)
            if m:
                raw_date = m.group(1)
                rest = line[m.end():].strip()

                if fmt == "MM/DD":
                    date_str = _parse_date_mm_dd(raw_date, year)
                elif fmt is not None:
                    try:
                        from datetime import datetime as _dt
                        date_str = _dt.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                else:
                    # Try both day-first and month-first
                    for _fmt in ("%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y",
                                 "%d %b %Y", "%d %B %Y", "%b %d, %Y"):
                        try:
                            from datetime import datetime as _dt
                            date_str = _dt.strptime(raw_date, _fmt).strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            continue

                if date_str:
                    prev_date = date_str
                    break

        if not date_str:
            # No date — skip unless we're accumulating a multi-line description
            continue

        # Strip optional transfer flag "& " (Chase lost/stolen account marker)
        rest = re.sub(r"^&\s+", "", rest)

        # Extract amount from end of line
        amt_match = _EOL_AMOUNT_RE.search(rest)
        if not amt_match:
            continue

        raw_amt = _normalise_amount(amt_match.group(1))
        try:
            amount = float(raw_amt)
        except ValueError:
            continue

        # Description is everything before the amount
        description = rest[: amt_match.start()].strip().strip("-").strip()
        if not description:
            description = "Transaction"

        # For sections marked as credits (payments/refunds), ensure amount is negative
        # (money coming in = negative charge on a credit card)
        # Only flip if the amount is unsigned / positive
        if in_credit_section and amount > 0:
            amount = -amount

        rows.append({
            "Date": date_str,
            "Description": description,
            "Amount": str(amount),
        })

    if not rows:
        return pd.DataFrame(columns=["Date", "Description", "Amount"])

    return pd.DataFrame(rows)
