"""PDF bank statement extraction.

Strategy (in order):
  1. Extract tables via pdfplumber — works well for PDFs with embedded table
     structure (most modern bank statements).
  2. Text-line heuristic — for scanned-style or column-aligned PDFs with no
     proper table structure: parse each line looking for date + description +
     amount patterns.

Returns a pandas DataFrame ready to be fed into the existing
detect_columns / import_transactions pipeline.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

# Regex that matches common date formats on a statement line
_DATE_RE = re.compile(
    r"\b(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})"   # DD/MM/YY, MM-DD-YYYY …
    r"|\b(\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})\b"   # YYYY-MM-DD
    r"|\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{2,4})\b"
    r"|\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}[,\s]+\d{4})\b",
    re.IGNORECASE,
)

# Regex that matches a monetary amount (with optional sign, currency symbol, commas)
_AMOUNT_RE = re.compile(
    r"[-+]?\s*[$£€¥]?\s*\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?"
    r"|[$£€¥]\s*\d+(?:\.\d{2})?"
)


# ── Public entry point ────────────────────────────────────────────────────────

def pdf_to_dataframe(filepath: str) -> pd.DataFrame:
    """Extract transactions from a PDF bank statement and return a DataFrame."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError(
            "pdfplumber is required for PDF import. "
            "Run: pip install pdfplumber"
        )

    with pdfplumber.open(filepath) as pdf:
        # Attempt 1: table extraction
        df = _extract_tables(pdf)
        if df is not None and len(df) >= 2:
            log.info("PDF table extraction succeeded: %d rows, %d cols",
                     len(df), len(df.columns))
            return df

        # Attempt 2: text-line parsing
        text = "\n".join(
            page.extract_text() or "" for page in pdf.pages
        )

    df = _parse_text_lines(text)
    log.info("PDF text-line extraction: %d rows", len(df))
    return df


# ── Table extraction ──────────────────────────────────────────────────────────

def _extract_tables(pdf) -> pd.DataFrame | None:
    """Collect all tables from every page, merge by column count, pick largest."""
    all_tables: list[list[list[Any]]] = []
    for page in pdf.pages:
        tables = page.extract_tables()
        if tables:
            all_tables.extend(tables)

    if not all_tables:
        return None

    # Group tables by column count
    by_ncols: dict[int, list[list[Any]]] = {}
    for tbl in all_tables:
        if not tbl:
            continue
        ncols = max(len(row) for row in tbl)
        by_ncols.setdefault(ncols, []).extend(tbl)

    # Pick the column count that appears in the most rows (likely the data table)
    best_ncols = max(by_ncols, key=lambda k: len(by_ncols[k]))
    rows = by_ncols[best_ncols]

    if not rows:
        return None

    # Treat first row as header if it looks non-numeric; otherwise generate names
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    looks_like_header = sum(
        1 for h in header
        if h and not re.match(r"^[-+$£€\d,.\s]+$", h)
    ) >= 2

    if looks_like_header:
        data_rows = rows[1:]
    else:
        header = [f"col_{i}" for i in range(best_ncols)]
        data_rows = rows

    # Normalise: pad / trim rows to header length, replace None with ""
    norm: list[list[str]] = []
    for row in data_rows:
        cells = [str(c).strip() if c is not None else "" for c in row]
        # Pad short rows
        while len(cells) < len(header):
            cells.append("")
        norm.append(cells[: len(header)])

    df = pd.DataFrame(norm, columns=header)
    # Drop rows that are entirely empty
    df = df[df.apply(lambda r: any(v.strip() for v in r), axis=1)]
    # Drop obvious sub-header / totals rows (all caps + few numeric cells)
    df = _drop_junk_rows(df)
    return df.reset_index(drop=True) if len(df) > 0 else None


def _drop_junk_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Remove rows that look like repeated headers or summary totals."""
    def is_junk(row: pd.Series) -> bool:
        vals = [str(v).strip() for v in row if str(v).strip()]
        if not vals:
            return True
        numeric_count = sum(1 for v in vals if re.match(r"^[-+$£€\d,.\s]+$", v))
        # A row that's all-uppercase with <2 numeric fields is probably a header repeat
        if all(v == v.upper() for v in vals) and numeric_count < 2:
            return True
        return False

    mask = df.apply(is_junk, axis=1)
    return df[~mask]


# ── Text-line extraction ──────────────────────────────────────────────────────

def _parse_text_lines(text: str) -> pd.DataFrame:
    """Parse a flat text blob into a Date / Description / Amount DataFrame."""
    rows: list[dict] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        date_match = _DATE_RE.search(line)
        if not date_match:
            continue

        date_str = next(g for g in date_match.groups() if g)

        # Find all amounts on this line
        amounts = _AMOUNT_RE.findall(line)
        if not amounts:
            continue

        # Remove the date from the line to isolate the description
        desc = _DATE_RE.sub("", line).strip()
        # Remove amount tokens from description
        for amt in amounts:
            desc = desc.replace(amt, "").strip()
        desc = re.sub(r"\s{2,}", " ", desc).strip(" |-,")
        if not desc:
            desc = "Transaction"

        # Use the last amount as the transaction amount (often after description)
        # Use second-to-last as balance if two amounts present
        amount_str = amounts[-1].strip()
        balance_str = amounts[-2].strip() if len(amounts) >= 2 else None

        row: dict = {
            "Date": date_str,
            "Description": desc,
            "Amount": amount_str,
        }
        if balance_str:
            row["Balance"] = balance_str

        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=["Date", "Description", "Amount"])

    return pd.DataFrame(rows)
