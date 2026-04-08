"""Transaction import from CSV / XLS — optimized for 1-10M row scale."""
import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.models.import_batch import ImportBatch, ImportStatus
from app.models.transaction import Transaction

log = logging.getLogger(__name__)

COMMON_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
]

CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY",
    "C$": "CAD", "A$": "AUD", "₹": "INR", "R$": "BRL",
    "₩": "KRW", "kr": "SEK", "Fr": "CHF", "zł": "PLN",
}


def read_file(filepath: str) -> pd.DataFrame:
    path = Path(filepath)
    ext = path.suffix.lower()
    if ext == ".csv":
        return pd.read_csv(filepath, low_memory=False)
    elif ext in (".xls", ".xlsx"):
        return pd.read_excel(filepath)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


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

    return mapping


def parse_date(value) -> datetime | None:
    if pd.isna(value):
        return None
    value = str(value).strip()
    for fmt in COMMON_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return pd.to_datetime(value).to_pydatetime()
    except Exception:
        return None


def parse_amount(value) -> float | None:
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
        result = float(s)
        return -result if parens else result
    except ValueError:
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
) -> ImportBatch:
    """Import transactions with batch flushing for large files.

    For liability accounts (credit cards, loans, mortgages), positive amounts
    in the file represent charges/debits and are stored as negative values
    so that the account balance correctly reflects money owed.
    """
    df = read_file(filepath)
    path = Path(filepath)
    total_rows = len(df)

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
    pending_objects: list[Transaction] = []

    currency_col = column_mapping.get("currency")

    for _, row in df.iterrows():
        date_val = parse_date(row.get(column_mapping.get("date", ""), ""))
        desc_val = str(row.get(column_mapping.get("description", ""), "")).strip()
        amount_val = parse_amount(row.get(column_mapping.get("amount", ""), ""))

        if date_val is None or amount_val is None or not desc_val:
            skipped += 1
            continue

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

        # Flip sign for liabilities so charges are negative and payments positive
        if is_liability:
            amount_val = -amount_val

        txn = Transaction(
            account_id=account_id,
            date=date_val,
            description=desc_val,
            amount=amount_val,
            original_currency=txn_currency,
            balance_after=balance_val,
            import_batch_id=batch.id,
            raw_data=raw,
        )
        pending_objects.append(txn)
        imported += 1

        if len(pending_objects) >= batch_size:
            db.add_all(pending_objects)
            db.flush()
            pending_objects.clear()
            log.info("Flushed %d / %d rows", imported, total_rows)

    if pending_objects:
        db.add_all(pending_objects)
        db.flush()

    batch.row_count = imported
    batch.status = ImportStatus.COMPLETED
    db.commit()
    db.refresh(batch)

    log.info(
        "Import complete: %d imported, %d skipped, batch_id=%d",
        imported, skipped, batch.id,
    )
    return batch


def preview_file(filepath: str, max_rows: int = 10) -> dict:
    """Return preview data for column mapping UI."""
    df = read_file(filepath)
    mapping = detect_columns(df)
    preview_df = df.head(max_rows)

    return {
        "columns": list(df.columns),
        "mapping": mapping,
        "preview": preview_df.fillna("").to_dict(orient="records"),
        "total_rows": len(df),
    }
