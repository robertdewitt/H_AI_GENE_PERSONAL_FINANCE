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

        # Attempt 1: mortgage statement — transaction activity table
        if _is_mortgage_statement(full_text):
            df = _parse_mortgage_transactions(pdf, full_text)
            if df is not None and len(df) > 0:
                log.info("Mortgage statement: %d transactions", len(df))
                return df

        # Attempt 2: proper table extraction
        df = _extract_tables(pdf)
        if df is not None and len(df) >= 2:
            # Merge Charges/Payments columns if present (mortgage/loan format)
            df = _merge_charges_payments(df)
            if _looks_like_transactions(df):
                log.info("PDF table extraction: %d rows, %d cols", len(df), len(df.columns))
                return df

    # Attempt 3: Mission Fed account statement format (Date Amount[-] Balance Desc)
    df = _parse_mission_fed_account(full_text)
    if df is not None and len(df) > 0:
        log.info("Mission Fed account format: %d transactions", len(df))
        return df

    # Attempt 4: generic text-line parsing (Chase, Amex, Barclays, etc.)
    df = _parse_statement_text(full_text)
    log.info("PDF text-line extraction: %d rows", len(df))
    return df


def extract_cc_metadata(filepath: str) -> dict | None:
    """Extract credit-card statement summary from a PDF.

    Returns a dict with any of:
      payment_due_date   – date the next payment is due (date)
      minimum_payment    – minimum payment amount (float)
      new_balance        – statement closing balance (float)
      statement_date     – statement closing date (date)
    Returns None if not a recognisable credit-card statement.
    """
    try:
        import pdfplumber
    except ImportError:
        return None

    try:
        with pdfplumber.open(filepath) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages[:2])
    except Exception:
        return None

    # Only attempt if it looks like a CC statement (not a mortgage/loan)
    if _is_mortgage_statement(full_text):
        return None
    if not re.search(
        r"payment\s*due\s*date|minimum\s*payment|new\s*balance|statement\s*closing",
        full_text, re.I,
    ):
        return None

    result: dict = {}

    # Payment due date
    m = re.search(
        r"payment\s*due\s*date[:\s]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
        full_text, re.I,
    )
    if m:
        from datetime import datetime as _dt
        for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%d/%m/%Y"):
            try:
                result["payment_due_date"] = _dt.strptime(m.group(1), fmt).date()
                break
            except ValueError:
                pass

    # Minimum payment
    m = re.search(
        r"(?:total\s*)?minimum\s*payment(?:\s*due)?\s+\$?([\d,]+\.?\d*)",
        full_text, re.I,
    )
    if m:
        try:
            result["minimum_payment"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # New balance / statement balance
    m = re.search(
        r"new\s*balance(?:\s*total)?\s+\$?([\d,]+\.?\d*)",
        full_text, re.I,
    )
    if m:
        try:
            result["new_balance"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Statement closing date
    m = re.search(
        r"statement\s*closing\s*date[:\s]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
        full_text, re.I,
    )
    if m:
        from datetime import datetime as _dt
        for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"):
            try:
                result["statement_date"] = _dt.strptime(m.group(1), fmt).date()
                break
            except ValueError:
                pass

    return result if result else None


def extract_mortgage_metadata(filepath: str) -> dict | None:
    """Extract key loan facts from a mortgage statement PDF.

    Returns a dict with any of the following keys found:
      outstanding_balance  – current principal balance (float)
      interest_rate        – annual rate as a decimal, e.g. 0.0425 (float)
      monthly_payment      – regular payment amount (float)
      original_balance     – original loan amount (float)
      remaining_term_months– months left on the loan (int)

    Returns None if the file is not a mortgage statement or nothing is found.
    """
    try:
        import pdfplumber
    except ImportError:
        return None

    try:
        with pdfplumber.open(filepath) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return None

    if not _is_mortgage_statement(full_text):
        return None

    result: dict = {}
    lines = full_text.splitlines()

    # ── Patterns ──────────────────────────────────────────────────────
    # Balance patterns: match a label line followed by or containing an amount
    # NOTE: pdfplumber often strips spaces between words in structured boxes,
    # producing e.g. "OutstandingPrincipal:" instead of "Outstanding Principal:".
    # All word-boundary \s+ are written as \s* so they match with or without spaces.
    _BALANCE_LABELS = re.compile(
        r"(?:outstanding\s*principal|principal\s*balance|current\s*balance"
        r"|remaining\s*balance|loan\s*balance|unpaid\s*balance"
        r"|current\s*principal\s*balance)[^\d$£€]*([£$€]?[\d,]+\.?\d*)",
        re.IGNORECASE,
    )
    _RATE_LABELS = re.compile(
        r"(?:interest\s*rate|annual\s*percentage\s*rate|apr|note\s*rate"
        r"|current\s*rate|your\s*rate)[^\d]*(\d+\.?\d*)\s*%",
        re.IGNORECASE,
    )
    _PAYMENT_LABELS = re.compile(
        r"(?:regular\s*(?:monthly\s*)?payment|monthly\s*payment|scheduled\s*payment"
        r"|payment\s*amount|total\s*payment\s*due|next\s*payment\s*amount"
        r"|instalment\s*amount|installment\s*amount"
        r"|monthly\s*installment)[^\d$£€]*([£$€]?[\d,]+\.?\d*)",
        re.IGNORECASE,
    )
    _ORIGINAL_LABELS = re.compile(
        r"(?:original\s*(?:loan\s*)?(?:amount|principal|balance)"
        r"|loan\s*amount)[^\d$£€]*([£$€]?[\d,]+\.?\d*)",
        re.IGNORECASE,
    )
    _TERM_LABELS = re.compile(
        r"(?:remaining\s*term|months\s*remaining|term\s*remaining)[^\d]*(\d+)\s*(?:months?)?",
        re.IGNORECASE,
    )
    _STATEMENT_DATE_LABELS = re.compile(
        r"(?:statement\s*date|as\s*of\s*date|closing\s*date|period\s*end(?:ing)?)"
        r"[:\s]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE,
    )

    # Scan full text with multi-line patterns
    m = _BALANCE_LABELS.search(full_text)
    if m:
        try:
            result["outstanding_balance"] = float(
                m.group(1).replace("£", "").replace("$", "").replace("€", "").replace(",", "")
            )
        except ValueError:
            pass

    m = _RATE_LABELS.search(full_text)
    if m:
        try:
            result["interest_rate"] = float(m.group(1)) / 100.0
        except ValueError:
            pass

    m = _PAYMENT_LABELS.search(full_text)
    if m:
        try:
            result["monthly_payment"] = float(
                m.group(1).replace("£", "").replace("$", "").replace("€", "").replace(",", "")
            )
        except ValueError:
            pass

    m = _ORIGINAL_LABELS.search(full_text)
    if m:
        try:
            result["original_balance"] = float(
                m.group(1).replace("£", "").replace("$", "").replace("€", "").replace(",", "")
            )
        except ValueError:
            pass

    m = _TERM_LABELS.search(full_text)
    if m:
        try:
            result["remaining_term_months"] = int(m.group(1))
        except ValueError:
            pass

    m = _STATEMENT_DATE_LABELS.search(full_text)
    if m:
        from datetime import datetime as _dt
        raw_d = m.group(1).strip()
        for _fmt in ("%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%B %d, %Y", "%B %d %Y",
                     "%b %d, %Y", "%b %d %Y", "%m/%d/%y"):
            try:
                result["statement_date"] = _dt.strptime(raw_d, _fmt)
                break
            except ValueError:
                pass

    # Fallback: look for key–value pairs on adjacent lines
    # e.g.  "Outstanding Principal:"  (line N)   "$200,225.14"  (line N+1)
    # This handles structured-box PDFs where pdfplumber splits label and value.
    needs_balance  = "outstanding_balance" not in result
    needs_rate     = "interest_rate" not in result
    needs_payment  = "monthly_payment" not in result

    if needs_balance or needs_rate or needs_payment:
        for i, line in enumerate(lines):
            line_l = line.strip().lower()
            next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
            # combine current + next line so inline and split-line formats both match
            combined = line.strip() + " " + next_line

            if needs_balance and re.search(
                r"outstanding\s*principal|principal\s*balance|current\s*balance"
                r"|remaining\s*balance|loan\s*balance|unpaid\s*balance",
                line_l,
            ):
                amt_m = re.search(r"[£$€]\s*([\d,]+\.?\d*)", combined)
                if not amt_m:
                    amt_m = re.search(r"\b([\d,]{4,}\.?\d*)\b", combined)
                if amt_m:
                    try:
                        result["outstanding_balance"] = float(
                            amt_m.group(1).replace(",", "")
                        )
                        needs_balance = False
                    except ValueError:
                        pass

            if needs_rate and re.search(
                r"interest\s*rate|note\s*rate|current\s*rate", line_l
            ):
                rate_m = re.search(r"(\d+\.?\d*)\s*%", combined)
                if rate_m:
                    try:
                        result["interest_rate"] = float(rate_m.group(1)) / 100.0
                        needs_rate = False
                    except ValueError:
                        pass

            if needs_payment and re.search(
                r"(?:regular|monthly|scheduled|instalment|installment)\s*(?:monthly\s*)?payment"
                r"|payment\s*amount",
                line_l,
            ):
                amt_m = re.search(r"[£$€]\s*([\d,]+\.?\d*)", combined)
                if not amt_m:
                    amt_m = re.search(r"\b([\d,]+\.\d{2})\b", combined)
                if amt_m:
                    try:
                        result["monthly_payment"] = float(
                            amt_m.group(1).replace(",", "")
                        )
                        needs_payment = False
                    except ValueError:
                        pass

    # ── Explanation of Amount Due — current payment breakdown ─────────────────
    # pdfplumber collapses spaces so "Explanation of Amount Due" →
    # "ExplanationofAmountDue".  Match both forms.
    _EXPL_SECTION_RE = re.compile(
        r"ExplanationofAmountDue|Explanation\s*of\s*Amount\s*Due",
        re.IGNORECASE,
    )
    _COMP_AMT_RE = re.compile(r"[£$€]?([\d,]+\.\d{2})")

    expl_match = _EXPL_SECTION_RE.search(full_text)
    if expl_match:
        # Slice text after the marker so we only look at the current-payment column
        after = full_text[expl_match.end():]

        # Principal
        p_m = re.search(
            r"Principal\s*:\s*[£$€]?([\d,]+\.\d{2})", after, re.IGNORECASE
        )
        if p_m:
            try:
                result["payment_principal"] = float(p_m.group(1).replace(",", ""))
                result["_decomp_confidence"] = 0.95
            except ValueError:
                pass

        # Interest
        i_m = re.search(
            r"Interest\s*:\s*[£$€]?([\d,]+\.\d{2})", after, re.IGNORECASE
        )
        if i_m:
            try:
                result["payment_interest"] = float(i_m.group(1).replace(",", ""))
            except ValueError:
                pass

        # Escrow (optional)
        e_m = re.search(
            r"Escrow\s*(?:\([^)]*\))?\s*:\s*[£$€]?([\d,]+\.\d{2})", after, re.IGNORECASE
        )
        if e_m:
            try:
                result["payment_escrow"] = float(e_m.group(1).replace(",", ""))
            except ValueError:
                pass

    # ── Fallback: derive principal/interest from loan parameters ─────────────
    if (
        "payment_principal" not in result
        and "outstanding_balance" in result
        and "interest_rate" in result
        and "monthly_payment" in result
    ):
        try:
            monthly_rate = result["interest_rate"] / 12
            interest = round(result["outstanding_balance"] * monthly_rate, 2)
            principal = round(result["monthly_payment"] - interest, 2)
            result["payment_interest"] = interest
            result["payment_principal"] = principal
            result["payment_escrow"] = 0.0
            result["_decomp_confidence"] = 0.80
        except Exception:
            pass

    # Ensure payment_escrow has a default if principal/interest were found
    if "payment_principal" in result and "payment_escrow" not in result:
        result["payment_escrow"] = 0.0

    return result if result else None


def _is_mortgage_statement(text: str) -> bool:
    return bool(re.search(r"mortgage\s+statement|loan\s+statement|outstanding\s+principal", text, re.I))


def _parse_mortgage_transactions(pdf, full_text: str) -> pd.DataFrame | None:
    """Extract the TransactionActivity table from a mortgage/loan statement."""
    year = _infer_year(full_text)
    rows: list[dict] = []

    for page in pdf.pages:
        for table in page.extract_tables():
            if not table or len(table) < 2:
                continue

            # Find the actual column header row — skip span/title rows
            header_row_idx = 0
            for ri, row in enumerate(table[:3]):
                cells = [str(c or "").strip().lower() for c in row]
                if any("date" in c for c in cells) and any(
                    c in ("charges", "payments", "amount", "credit", "debit", "description")
                    for c in cells
                ):
                    header_row_idx = ri
                    break
            else:
                continue   # no usable header found

            header = [str(c or "").strip().lower() for c in table[header_row_idx]]
            has_date = any("date" in h for h in header)
            has_amt = any(h in ("charges", "payments", "amount", "credit", "debit") for h in header)
            if not (has_date and has_amt):
                continue

            date_i = next(i for i, h in enumerate(header) if "date" in h)
            desc_i = next((i for i, h in enumerate(header) if "desc" in h), None)
            charge_i = next((i for i, h in enumerate(header) if h in ("charges", "debit")), None)
            payment_i = next((i for i, h in enumerate(header) if h in ("payments", "payment", "credit", "amount")), None)

            for row in table[header_row_idx + 1:]:
                if not row or not row[date_i]:
                    continue
                raw_date = str(row[date_i]).strip()
                if not re.match(r"\d{1,2}/\d{1,2}", raw_date):
                    continue
                date_str = _parse_date_mm_dd(raw_date, year)
                if not date_str:
                    continue

                desc = str(row[desc_i] or "").strip() if desc_i is not None else ""
                desc = desc.replace("\n", " ").strip() or "Payment"

                # Charges = money out (+), Payments = money in (-)
                amount_str = ""
                if charge_i is not None and row[charge_i]:
                    amount_str = str(row[charge_i]).strip()
                elif payment_i is not None and row[payment_i]:
                    amount_str = str(row[payment_i]).strip()
                    # Payments on a loan statement are credits (negative = money paid in)
                    # but for the loan account itself a payment reduces balance (positive)

                if not amount_str:
                    continue

                # Normalise: strip $, commas, handle suffix/prefix minus
                norm = _normalise_amount(amount_str.replace("$", "").replace(",", ""))
                # If it came from payments column, it's a debit on the bank account
                # but for loan purposes mark as positive (balance reduction)
                try:
                    amt = float(norm)
                except ValueError:
                    continue

                rows.append({"Date": date_str, "Description": desc, "Amount": str(amt)})

    if not rows:
        return None
    return pd.DataFrame(rows)


def _merge_charges_payments(df: pd.DataFrame) -> pd.DataFrame:
    """If df has Charges + Payments columns, merge into a single Amount column."""
    cols_lower = {c.lower(): c for c in df.columns}
    charge_col = cols_lower.get("charges") or cols_lower.get("debit")
    payment_col = cols_lower.get("payments") or cols_lower.get("payment") or cols_lower.get("credit")
    if not (charge_col and payment_col):
        return df

    def _pick_amount(row):
        c = str(row[charge_col]).strip().lstrip("$").replace(",", "") if row[charge_col] else ""
        p = str(row[payment_col]).strip().lstrip("$").replace(",", "") if row[payment_col] else ""
        # Non-empty charge → positive debit; payment → negative credit
        try:
            if c and float(c) != 0:
                return c
        except ValueError:
            pass
        try:
            if p and float(p) != 0:
                v = float(p)
                return str(-v) if v > 0 else str(v)
        except ValueError:
            pass
        return ""

    df = df.copy()
    df["Amount"] = df.apply(_pick_amount, axis=1)
    return df.drop(columns=[charge_col, payment_col])


# ── Mission Fed account statement format ─────────────────────────────────────
# Format per line: MM/DD  $AMOUNT[-]  $BALANCE  Description
_MF_LINE_RE = re.compile(
    r"^(\d{2}/\d{2})\s+\$?([\d,]+\.\d{2})(-?)\s+\$?([\d,]+\.\d{2})\s+(.*)"
)


def _parse_mission_fed_account(text: str) -> pd.DataFrame | None:
    """Parse Mission Federal Credit Union account statement text.

    Line format:  MM/DD  $AMOUNT[-]  $BALANCE  Description
    The trailing '-' on the amount indicates a withdrawal/debit.
    """
    if "Account Statement" not in text and "MissionFed" not in text:
        return None

    year = _infer_year(text)
    rows: list[dict] = []

    for line in text.splitlines():
        line = line.strip()
        m = _MF_LINE_RE.match(line)
        if not m:
            continue
        raw_date, amount_digits, minus_flag, _balance, desc = m.groups()
        date_str = _parse_date_mm_dd(raw_date, year)
        if not date_str:
            continue

        amount = float(amount_digits.replace(",", ""))
        if minus_flag == "-":
            amount = -amount   # debit
        # else: positive = credit/deposit

        desc = desc.strip() or "Transaction"
        rows.append({"Date": date_str, "Description": desc, "Amount": str(amount)})

    if not rows:
        return None
    return pd.DataFrame(rows)


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
# NOTE: only comma is allowed as thousands separator (not space) — space-separated
# digit groups like "4241 320.00" in BofA statements (card-suffix + amount) would
# otherwise be misread as a single large number.
_EOL_AMOUNT_RE = re.compile(
    r"([-−–+]?\s*[$£€¥]?\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s*$"
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
