"""Revolut GBP PDF statement parser.

Handles the multi-column layout emitted by Revolut's "Download Statement → PDF"
feature.  Each transaction occupies one bold main row followed by one or more
smaller sub-rows (Reference, To, From, Card, Revolut Rate).

Column x-boundaries determined from the standard Revolut PDF template:
    Date        x  <  120
    Description 120 ≤ x <  330
    Money out   330 ≤ x <  415
    Money in    415 ≤ x <  525
    Balance     525 ≤ x
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


# ── x-axis column boundaries ───────────────────────────────────────
_X_DESC_START = 120
_X_OUT_START  = 330
_X_IN_START   = 415
_X_BAL_X1_MIN = 520   # balance words have x1 ≥ this (right-aligned)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Sections that are ALWAYS skipped regardless of user selection.
# "Reverted from" = cancelled transactions already reflected in the main
# stream; importing would create duplicates.
# "Pending" = unsettled; balance column absent.
_ALWAYS_SKIP = re.compile(
    r"(^reverted from\b|^pending\b)",
    re.IGNORECASE,
)


# ── Section dataclass ───────────────────────────────────────────────

@dataclass
class RevolutSection:
    key: str            # slug used in form values, e.g. "main", "maya"
    display_name: str   # human-readable label shown to the user
    count: int          # number of transactions found in this section
    is_main: bool       # True for the primary account section


# ── Internal helpers ────────────────────────────────────────────────

def _parse_amount(text: str) -> Decimal | None:
    clean = text.strip()
    negative = clean.startswith("-")
    if negative:
        clean = clean[1:]
    clean = clean.lstrip("£$€¥").replace(",", "")
    try:
        val = Decimal(clean)
        return -val if negative else val
    except InvalidOperation:
        return None


def _parse_date(words: list[str]) -> datetime | None:
    if len(words) < 3:
        return None
    try:
        d = int(words[0])
        m = _MONTHS.get(words[1].lower())
        y = int(words[2])
        if m is None:
            return None
        return datetime(y, m, d)
    except (ValueError, TypeError):
        return None


def _identify_section(row_text: str) -> tuple[str, str, bool] | None:
    """Return (key, display_name, is_main) if the row starts a new section.

    Returns None for rows that are not section headers.
    Returns ("__skip__", ..., False) for sections that must always be skipped.
    """
    # Always-skip sections
    if _ALWAYS_SKIP.search(row_text):
        return ("__skip__", "", False)

    # Named person's account — e.g. "Maya's account transactions from…"
    m = re.search(
        r"^(.+?)(?:'s|[\u2019]s|\"s)\s+account\s+transactions",
        row_text, re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip()
        key  = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        return (key, f"{name}'s Account", False)

    # Main account
    if re.search(r"Account transactions from", row_text, re.IGNORECASE):
        return ("main", "Main Account", True)

    # Personal and group pockets
    if re.search(r"personal and group pockets", row_text, re.IGNORECASE):
        return ("pockets", "Personal & Group Pockets", False)

    # Savings
    if re.search(r"savings transactions", row_text, re.IGNORECASE):
        return ("savings", "Savings", False)

    return None


def _is_page_chrome(row_text: str, row_words: list[dict]) -> bool:
    """Return True if the row is a page header/footer we should ignore."""
    if any(
        w["text"] in ("Revolut", "Ltd") and float(w["x0"]) > 400
        for w in row_words
    ):
        return True
    return bool(re.search(
        r"Report lost|08804411|Financial Conduct|Electronic Money"
        r"|registered address|scan the QR|© 20\d\d|Page \d+ of \d+",
        row_text, re.IGNORECASE,
    ))


def _has_date(row_words: list[dict]) -> bool:
    """Return True if the row has words in the date column."""
    return any(float(w["x0"]) < _X_DESC_START for w in row_words)


def _extract_transaction(row_words: list[dict]) -> dict | None:
    """Parse one main-row into a transaction dict, or None if unparseable."""
    date_words_w = [w for w in row_words if float(w["x0"]) < _X_DESC_START]
    if not date_words_w:
        return None

    txn_date = _parse_date([w["text"] for w in date_words_w])
    if txn_date is None:
        return None

    bal_ids  = {id(w) for w in row_words
                if float(w.get("x1", w["x0"])) >= _X_BAL_X1_MIN}
    bal_words = [w["text"] for w in row_words
                 if float(w.get("x1", w["x0"])) >= _X_BAL_X1_MIN]
    out_words = [w["text"] for w in row_words
                 if _X_OUT_START <= float(w["x0"]) < _X_IN_START
                 and id(w) not in bal_ids]
    in_words  = [w["text"] for w in row_words
                 if float(w["x0"]) >= _X_IN_START
                 and id(w) not in bal_ids]
    desc_words = [w["text"] for w in row_words
                  if _X_DESC_START <= float(w["x0"]) < _X_OUT_START]

    money_out = _parse_amount(" ".join(out_words)) if out_words else None
    money_in  = _parse_amount(" ".join(in_words))  if in_words  else None
    balance   = _parse_amount(" ".join(bal_words))  if bal_words  else None

    if money_out is None and money_in is None:
        return None

    amount = -money_out if money_out is not None else money_in  # type: ignore[assignment]
    description = " ".join(desc_words).strip()
    if not description:
        return None

    return {
        "date": txn_date,
        "description": description,
        "amount": amount,
        "balance": balance,
    }


# ── Column-header row detector ─────────────────────────────────────

def _is_column_header(row_text: str) -> bool:
    return bool(re.search(r"\bDate\b.*\bDescription\b.*\bBalance\b", row_text))


# ── Public API ─────────────────────────────────────────────────────

def is_revolut_pdf(path: str) -> bool:
    """Quick check — returns True if the file looks like a Revolut statement."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = pdf.pages[0].extract_text() or ""
            return "Revolut" in text and (
                "Money out" in text or "Account transactions" in text
            )
    except Exception:
        return False


def detect_revolut_sections(path: str) -> list[RevolutSection]:
    """Scan the PDF and return one RevolutSection per importable section.

    Sections are returned in document order.  The "Reverted from" and
    "Pending" sections are never returned — they are always skipped.
    """
    import pdfplumber

    sections: list[RevolutSection] = []
    # Track (key → index into `sections`) so we can accumulate counts
    key_index: dict[str, int] = {}
    current_key: str | None = None   # None = haven't entered any section yet
    skip_active = False

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, extra_attrs=["height"])

            rows: dict[int, list[dict]] = {}
            for w in words:
                y_key = round(float(w["top"]) / 2) * 2
                rows.setdefault(y_key, []).append(w)

            for y_key in sorted(rows):
                row_words = sorted(rows[y_key], key=lambda w: float(w["x0"]))
                row_text  = " ".join(w["text"] for w in row_words)

                if _is_page_chrome(row_text, row_words):
                    continue
                if _is_column_header(row_text):
                    continue

                sec = _identify_section(row_text)
                if sec is not None:
                    key, display_name, is_main = sec
                    if key == "__skip__":
                        skip_active = True
                        current_key = None
                        continue
                    skip_active = False
                    current_key = key
                    if key not in key_index:
                        key_index[key] = len(sections)
                        sections.append(RevolutSection(
                            key=key,
                            display_name=display_name,
                            count=0,
                            is_main=is_main,
                        ))
                    continue

                if skip_active or current_key is None:
                    continue

                # Count main transaction rows (ones that have a parseable date)
                date_col = [w for w in row_words if float(w["x0"]) < _X_DESC_START]
                if date_col and _parse_date([w["text"] for w in date_col]) is not None:
                    sections[key_index[current_key]].count += 1

    return sections


def parse_revolut_pdf(
    path: str,
    include_sections: set[str] | None = None,
) -> list[dict]:
    """Parse transactions from the specified sections of a Revolut PDF.

    Args:
        path:             Path to the PDF file.
        include_sections: Set of section keys to import (e.g. {"main"}).
                          Defaults to {"main"} when None.

    Returns:
        List of dicts with keys: date, description, amount, balance, section.
    """
    import pdfplumber

    if include_sections is None:
        include_sections = {"main"}

    transactions: list[dict] = []
    current_key: str | None = None
    skip_active = False

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, extra_attrs=["height"])

            rows: dict[int, list[dict]] = {}
            for w in words:
                y_key = round(float(w["top"]) / 2) * 2
                rows.setdefault(y_key, []).append(w)

            for y_key in sorted(rows):
                row_words = sorted(rows[y_key], key=lambda w: float(w["x0"]))
                row_text  = " ".join(w["text"] for w in row_words)

                if _is_page_chrome(row_text, row_words):
                    continue

                sec = _identify_section(row_text)
                if sec is not None:
                    key, _, _ = sec
                    if key == "__skip__":
                        skip_active = True
                        current_key = None
                    else:
                        skip_active = False
                        current_key = key
                    continue

                if _is_column_header(row_text):
                    continue

                if skip_active or current_key is None:
                    continue

                if current_key not in include_sections:
                    continue

                # Sub-rows (no date column) — skip
                if not any(float(w["x0"]) < _X_DESC_START for w in row_words):
                    continue

                txn = _extract_transaction(row_words)
                if txn is not None:
                    txn["section"] = current_key
                    transactions.append(txn)

    return transactions
