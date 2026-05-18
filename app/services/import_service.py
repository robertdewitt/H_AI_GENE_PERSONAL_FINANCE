"""Transaction import from CSV / XLS — optimized for 1-10M row scale."""
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.import_batch import ImportBatch, ImportStatus
from app.models.transaction import Transaction

log = logging.getLogger(__name__)

DATE_FORMATS_DAYFIRST = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%d-%m-%Y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
    "%m/%d/%y",
]

DATE_FORMATS_MONTHFIRST = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
]

_SEP_PATTERN = re.compile(r"^(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{2,4})$")


@dataclass
class DateFormatDetection:
    dayfirst: bool
    confidence: str  # "high", "medium", "low"
    reasoning: list[str] = field(default_factory=list)
    sample_parsed: list[dict] = field(default_factory=list)
    format_label: str = ""

    def __post_init__(self):
        if not self.format_label:
            self.format_label = "DD/MM/YYYY" if self.dayfirst else "MM/DD/YYYY"


def detect_date_format(
    raw_values: list,
    sample_size: int = 200,
) -> DateFormatDetection:
    """Scan date values and determine whether they are day-first or month-first.

    Uses multiple signals: values > 12 that can only be a day, consistency
    of sequential ordering, and statistical spread of each position.
    """
    cleaned = []
    for v in raw_values:
        if pd.isna(v):
            continue
        s = str(v).strip()
        if s:
            cleaned.append(s)
    if not cleaned:
        return DateFormatDetection(
            dayfirst=settings.date_dayfirst,
            confidence="low",
            reasoning=["No date values found; using default setting."],
        )

    sample = cleaned[:sample_size]
    reasoning: list[str] = []

    # Named-month detection (e.g. "04 Mar 2025", "Mar 04, 2025")
    named_month_count = sum(
        1 for s in sample
        if re.search(r"[A-Za-z]{3,}", s)
    )
    if named_month_count > len(sample) * 0.8:
        reasoning.append(
            f"{named_month_count}/{len(sample)} dates contain month names "
            f"— format is unambiguous."
        )
        return DateFormatDetection(
            dayfirst=False,
            confidence="high",
            reasoning=reasoning,
            format_label="Named month (e.g. Mar 04, 2025)",
        )

    # ISO detection (YYYY-MM-DD or YYYY/MM/DD)
    iso_count = sum(1 for s in sample if re.match(r"^\d{4}[/\-]", s))
    if iso_count > len(sample) * 0.8:
        reasoning.append(
            f"{iso_count}/{len(sample)} dates start with 4-digit year "
            f"— ISO/year-first format."
        )
        return DateFormatDetection(
            dayfirst=False,
            confidence="high",
            reasoning=reasoning,
            format_label="YYYY-MM-DD (ISO)",
        )

    # Separator-based analysis (DD/MM/YY, MM/DD/YYYY, etc.)
    part1_vals: list[int] = []
    part2_vals: list[int] = []
    parsed_rows: list[tuple[int, int, int]] = []

    for s in sample:
        m = _SEP_PATTERN.match(s)
        if m:
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a > 31:
                continue  # year-first, already handled
            part1_vals.append(a)
            part2_vals.append(b)
            parsed_rows.append((a, b, c))

    if not parsed_rows:
        reasoning.append(
            "Could not extract numeric date parts; "
            "falling back to config default."
        )
        return DateFormatDetection(
            dayfirst=settings.date_dayfirst,
            confidence="low",
            reasoning=reasoning,
        )

    reasoning.append(f"Scanned {len(parsed_rows)} numeric dates.")

    # Signal 1: Values > 12 can ONLY be a day, never a month
    p1_over_12 = sum(1 for v in part1_vals if v > 12)
    p2_over_12 = sum(1 for v in part2_vals if v > 12)

    if p1_over_12 > 0 and p2_over_12 == 0:
        reasoning.append(
            f"Position 1 has {p1_over_12} values > 12 (must be day); "
            f"position 2 has none → day-first (DD/MM)."
        )
        return _build_result(
            dayfirst=True, confidence="high",
            reasoning=reasoning, sample=sample,
        )

    if p2_over_12 > 0 and p1_over_12 == 0:
        reasoning.append(
            f"Position 2 has {p2_over_12} values > 12 (must be day); "
            f"position 1 has none → month-first (MM/DD)."
        )
        return _build_result(
            dayfirst=False, confidence="high",
            reasoning=reasoning, sample=sample,
        )

    if p1_over_12 > 0 and p2_over_12 > 0:
        reasoning.append(
            f"Both positions have values > 12 ({p1_over_12} / {p2_over_12}) "
            f"— mixed formats or parsing error. Using majority signal."
        )
        dayfirst = p1_over_12 >= p2_over_12
        return _build_result(
            dayfirst=dayfirst, confidence="medium",
            reasoning=reasoning, sample=sample,
        )

    # Signal 2: All values ≤ 12 in both positions — truly ambiguous.
    # Use value range: days typically span 1-28/31, months 1-12.
    # The position with wider spread is more likely to be the day.
    p1_unique = len(set(part1_vals))
    p2_unique = len(set(part2_vals))
    p1_max = max(part1_vals)
    p2_max = max(part2_vals)

    reasoning.append(
        f"All values ≤ 12 in both positions — ambiguous. "
        f"Analyzing distribution: "
        f"pos1 range 1–{p1_max} ({p1_unique} unique), "
        f"pos2 range 1–{p2_max} ({p2_unique} unique)."
    )

    # Signal 3: If dates should be roughly chronological, try both
    # interpretations and see which gives better ordering
    dmy_sorted = 0
    mdy_sorted = 0
    for i in range(1, len(parsed_rows)):
        a1, b1, c1 = parsed_rows[i - 1]
        a2, b2, c2 = parsed_rows[i]
        # As day-first: date is (year=c, month=b, day=a)
        dmy1 = (c1, b1, a1)
        dmy2 = (c2, b2, a2)
        if dmy2 >= dmy1:
            dmy_sorted += 1
        # As month-first: date is (year=c, month=a, day=b)
        mdy1 = (c1, a1, b1)
        mdy2 = (c2, a2, b2)
        if mdy2 >= mdy1:
            mdy_sorted += 1

    total_pairs = max(len(parsed_rows) - 1, 1)
    dmy_pct = dmy_sorted / total_pairs
    mdy_pct = mdy_sorted / total_pairs

    reasoning.append(
        f"Chronological ordering test: "
        f"DD/MM interprets {dmy_pct:.0%} in order, "
        f"MM/DD interprets {mdy_pct:.0%} in order."
    )

    if abs(dmy_pct - mdy_pct) > 0.15:
        dayfirst = dmy_pct > mdy_pct
        reasoning.append(
            f"{'DD/MM' if dayfirst else 'MM/DD'} gives significantly "
            f"better chronological ordering."
        )
        return _build_result(
            dayfirst=dayfirst, confidence="medium",
            reasoning=reasoning, sample=sample,
        )

    # Signal 4: Position with more unique values is likely the day
    if p1_unique != p2_unique:
        dayfirst = p1_unique > p2_unique
        reasoning.append(
            f"Position {'1' if dayfirst else '2'} has more unique values "
            f"({max(p1_unique, p2_unique)} vs {min(p1_unique, p2_unique)}), "
            f"likely the day field → {'DD/MM' if dayfirst else 'MM/DD'}."
        )
        return _build_result(
            dayfirst=dayfirst, confidence="low",
            reasoning=reasoning, sample=sample,
        )

    # Truly indeterminate — use config default
    reasoning.append(
        f"Cannot distinguish; defaulting to "
        f"{'DD/MM (UK)' if settings.date_dayfirst else 'MM/DD (US)'}."
    )
    return _build_result(
        dayfirst=settings.date_dayfirst, confidence="low",
        reasoning=reasoning, sample=sample,
    )


def _build_result(
    dayfirst: bool,
    confidence: str,
    reasoning: list[str],
    sample: list[str],
) -> DateFormatDetection:
    sample_parsed = []
    for s in sample[:5]:
        dmy = _try_parse(s, dayfirst=True)
        mdy = _try_parse(s, dayfirst=False)
        sample_parsed.append({
            "raw": s,
            "as_dmy": dmy.strftime("%d %b %Y") if dmy else "?",
            "as_mdy": mdy.strftime("%d %b %Y") if mdy else "?",
        })
    return DateFormatDetection(
        dayfirst=dayfirst,
        confidence=confidence,
        reasoning=reasoning,
        sample_parsed=sample_parsed,
    )


def _try_parse(value: str, dayfirst: bool) -> datetime | None:
    fmts = DATE_FORMATS_DAYFIRST if dayfirst else DATE_FORMATS_MONTHFIRST
    for fmt in fmts:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return pd.to_datetime(value, dayfirst=dayfirst).to_pydatetime()
    except Exception:
        return None

CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY",
    "C$": "CAD", "A$": "AUD", "₹": "INR", "R$": "BRL",
    "₩": "KRW", "kr": "SEK", "Fr": "CHF", "zł": "PLN",
}


HEADER_HINTS = {
    "date", "transaction date", "posted", "posting date", "trans date",
    "trade date", "settlement date",
    "description", "memo", "narrative", "details", "payee",
    "transaction description", "name", "reference",
    "amount", "value", "sum", "transaction amount", "debit/credit",
    "net amount", "debit", "credit",
    "balance", "running balance", "balance after", "available",
    "currency", "ccy", "cur", "currency code",
    "type", "category", "status", "check number",
}


def _find_header_row(filepath: str, ext: str, max_scan: int = 30) -> int | None:
    """Scan the first *max_scan* lines to find which one looks like a header.

    Returns the 0-based row index, or None if row 0 already looks correct.
    A row is considered a header if at least 2 of its cell values (lowered)
    match known column header keywords.

    For CSVs, reads raw lines to avoid parser errors from ragged metadata rows.
    """
    if ext == ".csv":
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                lines = []
                for i, line in enumerate(fh):
                    if i >= max_scan:
                        break
                    lines.append(line)
        except Exception:
            return None

        for row_idx, line in enumerate(lines):
            cells = [c.strip().strip('"').lower() for c in line.split(",")]
            cells = [c for c in cells if c]
            if _row_looks_like_header(cells):
                return row_idx
    else:
        try:
            raw = pd.read_excel(filepath, header=None, nrows=max_scan,
                                dtype=str, keep_default_na=False)
        except Exception:
            return None

        for row_idx in range(len(raw)):
            cells = [str(v).strip().lower() for v in raw.iloc[row_idx]
                     if str(v).strip()]
            if _row_looks_like_header(cells):
                return row_idx

    return None


def _row_looks_like_header(cells: list[str]) -> bool:
    """Return True if at least 2 cells match known header keywords."""
    if len(cells) < 2:
        return False
    matches = sum(1 for c in cells if c in HEADER_HINTS)
    if matches < 2:
        partial_hints = ("date", "desc", "amount", "balance", "narr", "memo")
        matches += sum(
            1 for c in cells
            if any(h in c for h in partial_hints)
            and c not in HEADER_HINTS
        )
    return matches >= 2


def read_file(filepath: str) -> pd.DataFrame:
    path = Path(filepath)
    ext = path.suffix.lower()
    if ext not in (".csv", ".xls", ".xlsx", ".pdf"):
        raise ValueError(f"Unsupported file type: {ext}")

    if ext == ".pdf":
        from app.services.pdf_import import pdf_to_dataframe
        return pdf_to_dataframe(filepath)

    header_row = _find_header_row(filepath, ext)
    if header_row is None:
        header_row = 0

    if header_row > 0:
        log.info(
            "Skipping %d metadata row(s) in %s — header found on row %d",
            header_row, path.name, header_row,
        )

    skip = list(range(header_row)) if header_row > 0 else None

    if ext == ".csv":
        return pd.read_csv(filepath, skiprows=skip, low_memory=False)
    else:
        return pd.read_excel(filepath, skiprows=skip)


def detect_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """Heuristic mapping of DataFrame columns to our schema."""
    col_lower = {c: c.lower().strip() for c in df.columns}
    mapping: dict[str, str | None] = {
        "date": None, "description": None,
        "amount": None, "balance": None, "currency": None,
    }

    date_hints = [
        "date", "transaction date", "posted", "posting date",
        "trans date", "trade date", "settlement date",
    ]
    desc_hints = [
        "description", "memo", "narrative", "details", "payee",
        "transaction description", "name", "reference",
    ]
    amount_hints = [
        "amount", "value", "sum", "transaction amount",
        "debit/credit", "net amount",
    ]
    balance_hints = [
        "balance", "running balance", "balance after", "available",
    ]
    currency_hints = [
        "currency", "ccy", "cur", "currency code",
    ]
    debit_hints = ["debit", "withdrawal", "debit amount", "withdrawals", "dr"]
    credit_hints = ["credit", "deposit", "credit amount", "deposits", "cr"]

    mapping: dict[str, str | None] = {
        "date": None, "description": None,
        "amount": None, "balance": None, "currency": None,
        "debit": None, "credit": None,
    }

    for original, lower in col_lower.items():
        if mapping["date"] is None and lower in date_hints:
            mapping["date"] = original
        if mapping["description"] is None and lower in desc_hints:
            mapping["description"] = original
        if mapping["amount"] is None and lower in amount_hints:
            mapping["amount"] = original
        if mapping["balance"] is None and lower in balance_hints:
            mapping["balance"] = original
        if mapping["currency"] is None and lower in currency_hints:
            mapping["currency"] = original
        if mapping["debit"] is None and lower in debit_hints:
            mapping["debit"] = original
        if mapping["credit"] is None and lower in credit_hints:
            mapping["credit"] = original

    # Fallbacks — substring matching
    for original, lower in col_lower.items():
        if mapping["date"] is None and "date" in lower:
            mapping["date"] = original
            break
    for original, lower in col_lower.items():
        if mapping["description"] is None and (
            "desc" in lower or "memo" in lower or "narr" in lower
        ):
            mapping["description"] = original
            break
    for original, lower in col_lower.items():
        if mapping["amount"] is None and (
            "amount" in lower or "amt" in lower
        ):
            mapping["amount"] = original
            break

    # If we found separate debit/credit but no combined amount column,
    # leave amount=None so the UI defaults to debit/credit mode.
    # If there's a combined amount column, debit/credit are still exposed
    # so the user can override.
    return mapping


def parse_date(value, dayfirst: bool | None = None) -> datetime | None:
    if pd.isna(value):
        return None
    value = str(value).strip()

    if dayfirst is None:
        dayfirst = settings.date_dayfirst

    formats = DATE_FORMATS_DAYFIRST if dayfirst else DATE_FORMATS_MONTHFIRST

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return pd.to_datetime(value, dayfirst=dayfirst).to_pydatetime()
    except Exception:
        return None


def parse_amount(value) -> Decimal | None:
    if pd.isna(value):
        return None
    s = str(value).strip()
    # Strip known currency symbols
    for sym in CURRENCY_SYMBOLS:
        s = s.replace(sym, "")
    s = s.replace(",", "").strip()
    parens = s.startswith("(") and s.endswith(")")
    if parens:
        s = s[1:-1]
    try:
        result = Decimal(s)
        return -result if parens else result
    except (InvalidOperation, ValueError):
        return None


def detect_currency_from_value(value) -> str | None:
    """Try to detect currency from a raw cell value like '$100' or '€50'."""
    if pd.isna(value):
        return None
    s = str(value).strip()
    for sym, ccy in CURRENCY_SYMBOLS.items():
        if s.startswith(sym) or s.endswith(sym):
            return ccy
    return None


def import_transactions(
    db: Session,
    account_id: int,
    filepath: str,
    column_mapping: dict[str, str],
    account_currency: str = "USD",
    is_liability: bool = False,
    dayfirst: bool | None = None,
) -> ImportBatch:
    """Import transactions with batch flushing for large files.

    For liability accounts (credit cards, loans, mortgages), positive amounts
    in the file represent charges/debits and are stored as negative values
    so that the account balance correctly reflects money owed.

    If *dayfirst* is None the format is auto-detected from the date column.
    """
    df = read_file(filepath)
    path = Path(filepath)
    total_rows = len(df)

    # Auto-detect date format from the data if not explicitly provided
    date_col_name = column_mapping.get("date", "")
    if dayfirst is None and date_col_name in df.columns:
        detection = detect_date_format(df[date_col_name].tolist())
        dayfirst = detection.dayfirst
        log.info(
            "Date format auto-detected: %s (%s confidence) — %s",
            detection.format_label, detection.confidence,
            "; ".join(detection.reasoning),
        )

    if dayfirst is None:
        dayfirst = settings.date_dayfirst

    batch = ImportBatch(
        account_id=account_id,
        filename=path.name,
        file_type=path.suffix.lstrip(".").lower(),
        row_count=total_rows,
        source="manual_upload",
        status=ImportStatus.PENDING,
    )
    db.add(batch)
    db.flush()

    batch_size = settings.import_batch_size
    imported = 0
    skipped = 0
    duplicates = 0
    pending_objects: list[Transaction] = []
    all_imported_ids: list[int] = []

    # Pre-load existing (date, description, amount) keys for this account
    # so duplicate detection is O(1) per row instead of a DB query each time.
    existing_rows = db.execute(
        select(
            Transaction.date,
            Transaction.description,
            Transaction.amount,
        ).where(Transaction.account_id == account_id)
    ).all()
    existing_keys: set[tuple] = {
        (r.date.strftime("%Y-%m-%d"), r.description.strip().lower(), round(r.amount, 2))
        for r in existing_rows
    }
    # Also track keys added during this import to catch in-file duplicates
    new_keys: set[tuple] = set()

    currency_col = column_mapping.get("currency")
    amount_col = column_mapping.get("amount")
    debit_col = column_mapping.get("debit")
    credit_col = column_mapping.get("credit")

    for _, row in df.iterrows():
        date_val = parse_date(
            row.get(date_col_name, ""), dayfirst=dayfirst,
        )
        desc_val = str(row.get(column_mapping.get("description", ""), "")).strip()

        if amount_col:
            amount_val = parse_amount(row.get(amount_col, ""))
        elif debit_col or credit_col:
            debit_raw = parse_amount(row.get(debit_col, "")) if debit_col else None
            credit_raw = parse_amount(row.get(credit_col, "")) if credit_col else None
            from decimal import Decimal as _D
            debit_amt = abs(debit_raw) if debit_raw is not None else _D("0")
            credit_amt = abs(credit_raw) if credit_raw is not None else _D("0")
            if debit_amt == 0 and credit_amt == 0:
                amount_val = None
            else:
                amount_val = credit_amt - debit_amt
        else:
            amount_val = None

        if date_val is None or amount_val is None or not desc_val:
            skipped += 1
            continue

        # Flip sign for liabilities so charges are negative and payments positive
        if is_liability:
            amount_val = -amount_val

        # Duplicate check uses the final stored amount
        dedup_key = (
            date_val.strftime("%Y-%m-%d"),
            desc_val.strip().lower(),
            round(amount_val, 2),
        )
        if dedup_key in existing_keys or dedup_key in new_keys:
            duplicates += 1
            continue
        new_keys.add(dedup_key)

        balance_col = column_mapping.get("balance")
        balance_val = parse_amount(row.get(balance_col, "")) if balance_col else None

        # Determine transaction currency
        txn_currency = account_currency
        if currency_col and not pd.isna(row.get(currency_col, "")):
            txn_currency = str(row[currency_col]).strip().upper()
        else:
            detected = detect_currency_from_value(
                row.get(column_mapping.get("amount", ""), "")
            )
            if detected:
                txn_currency = detected

        raw = json.dumps(
            {str(k): str(v) for k, v in row.items()},
            default=str,
        )

        # Auto-detect payments on liability accounts as transfers
        is_payment_transfer = False
        if is_liability and amount_val > 0:
            desc_lower = desc_val.lower()
            payment_keywords = [
                "payment", "autopay", "thank you", "pymt",
                "online pmt", "ach", "transfer",
            ]
            if any(kw in desc_lower for kw in payment_keywords):
                is_payment_transfer = True

        txn = Transaction(
            account_id=account_id,
            date=date_val,
            description=desc_val,
            amount=amount_val,
            original_currency=txn_currency,
            balance_after=balance_val,
            import_batch_id=batch.id,
            raw_data=raw,
            is_transfer=is_payment_transfer,
        )
        pending_objects.append(txn)
        imported += 1

        if len(pending_objects) >= batch_size:
            db.add_all(pending_objects)
            db.flush()
            all_imported_ids.extend(t.id for t in pending_objects)
            pending_objects.clear()
            log.info("Flushed %d / %d rows", imported, total_rows)

    if pending_objects:
        db.add_all(pending_objects)
        db.flush()
        all_imported_ids.extend(t.id for t in pending_objects)

    batch.row_count = imported
    batch.status = ImportStatus.COMPLETED
    db.commit()
    db.refresh(batch)

    # Classify newly imported transactions
    try:
        from app.services.event_classifier import classify_batch
        from app.services.split_auto import ensure_splits_after_import
        if all_imported_ids:
            classify_batch(db, transaction_ids=all_imported_ids)
            db.commit()
            ensure_splits_after_import(db, all_imported_ids)
            db.commit()
    except Exception:
        log.warning("Event classification failed for batch %d", batch.id, exc_info=True)

    log.info(
        "Import complete: %d imported, %d skipped, %d duplicates, batch_id=%d",
        imported, skipped, duplicates, batch.id,
    )
    batch._duplicates_skipped = duplicates
    return batch


def preview_file(filepath: str, max_rows: int = 10) -> dict:
    """Return preview data for column mapping UI."""
    df = read_file(filepath)
    mapping = detect_columns(df)
    preview_df = df.head(max_rows)

    # Auto-detect date format from the mapped date column
    date_detection = None
    if mapping.get("date") and mapping["date"] in df.columns:
        date_detection = detect_date_format(df[mapping["date"]].tolist())

    return {
        "columns": list(df.columns),
        "mapping": mapping,
        "preview": preview_df.fillna("").to_dict(orient="records"),
        "total_rows": len(df),
        "date_detection": date_detection,
    }
