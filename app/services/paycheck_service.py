"""Parse and manage paycheck stubs — CSV/XLS and future PDF/OCR."""
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.paycheck_stub import PaycheckStub
from app.services.import_service import parse_amount, parse_date, read_file


PAYCHECK_COLUMN_HINTS = {
    "pay_date": ["pay date", "check date", "payment date", "date"],
    "period_start": ["period start", "pay period start", "start date", "begin"],
    "period_end": ["period end", "pay period end", "end date"],
    "gross_pay": ["gross pay", "gross", "gross earnings", "total earnings"],
    "net_pay": ["net pay", "net", "take home", "net earnings"],
    "federal_tax": ["federal tax", "federal", "fed tax", "federal withholding"],
    "state_tax": ["state tax", "state", "state withholding"],
    "local_tax": ["local tax", "local", "city tax"],
    "social_security": ["social security", "fica", "ss", "oasdi"],
    "medicare": ["medicare", "med tax"],
    "retirement_401k": ["401k", "401(k)", "retirement", "pension"],
    "health_insurance": ["health", "medical", "health insurance"],
    "dental_insurance": ["dental", "dental insurance"],
    "vision_insurance": ["vision", "vision insurance"],
    "hsa_contribution": ["hsa", "health savings"],
    "employer": ["employer", "company"],
}


def detect_paycheck_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """Auto-detect which columns map to paycheck fields."""
    col_lower = {c: c.lower().strip() for c in df.columns}
    mapping: dict[str, str | None] = {k: None for k in PAYCHECK_COLUMN_HINTS}

    for field, hints in PAYCHECK_COLUMN_HINTS.items():
        for original, lower in col_lower.items():
            if mapping[field] is not None:
                break
            if lower in hints:
                mapping[field] = original
            elif any(h in lower for h in hints):
                mapping[field] = original

    return mapping


def preview_paycheck_file(filepath: str, max_rows: int = 10) -> dict:
    df = read_file(filepath)
    mapping = detect_paycheck_columns(df)
    preview_df = df.head(max_rows)
    return {
        "columns": list(df.columns),
        "mapping": mapping,
        "preview": preview_df.fillna("").to_dict(orient="records"),
        "total_rows": len(df),
    }


def import_paycheck_stubs(
    db: Session,
    account_id: int,
    filepath: str,
    column_mapping: dict[str, str],
) -> int:
    """Import paycheck stubs from a CSV/XLS file. Returns count imported."""
    df = read_file(filepath)
    imported = 0
    filename = Path(filepath).name

    for _, row in df.iterrows():
        pay_date = parse_date(
            row.get(column_mapping.get("pay_date", ""), "")
        )
        gross = parse_amount(
            row.get(column_mapping.get("gross_pay", ""), "")
        )
        net = parse_amount(
            row.get(column_mapping.get("net_pay", ""), "")
        )

        if pay_date is None or gross is None or net is None:
            continue

        def _get_float(field: str) -> float:
            col = column_mapping.get(field)
            if not col:
                return 0.0
            val = parse_amount(row.get(col, ""))
            return val if val is not None else 0.0

        def _get_date(field: str) -> datetime | None:
            col = column_mapping.get(field)
            if not col:
                return None
            return parse_date(row.get(col, ""))

        employer_col = column_mapping.get("employer")
        employer = str(row.get(employer_col, "")).strip() if employer_col else None

        stub = PaycheckStub(
            account_id=account_id,
            pay_date=pay_date,
            pay_period_start=_get_date("period_start"),
            pay_period_end=_get_date("period_end"),
            employer=employer or None,
            gross_pay=gross,
            net_pay=net,
            federal_tax=_get_float("federal_tax"),
            state_tax=_get_float("state_tax"),
            local_tax=_get_float("local_tax"),
            social_security=_get_float("social_security"),
            medicare=_get_float("medicare"),
            retirement_401k=_get_float("retirement_401k"),
            health_insurance=_get_float("health_insurance"),
            dental_insurance=_get_float("dental_insurance"),
            vision_insurance=_get_float("vision_insurance"),
            hsa_contribution=_get_float("hsa_contribution"),
            source_filename=filename,
        )
        db.add(stub)
        imported += 1

    db.commit()
    return imported


def create_paycheck_manual(
    db: Session,
    account_id: int,
    data: dict,
) -> PaycheckStub:
    """Create a single paycheck stub from manual form entry."""
    stub = PaycheckStub(account_id=account_id, **data)
    db.add(stub)
    db.commit()
    db.refresh(stub)
    return stub


def list_paychecks(
    db: Session,
    account_id: int | None = None,
    limit: int = 50,
) -> list[PaycheckStub]:
    query = select(PaycheckStub).order_by(PaycheckStub.pay_date.desc())
    if account_id:
        query = query.where(PaycheckStub.account_id == account_id)
    return db.execute(query.limit(limit)).scalars().all()


def get_paycheck_summary(
    db: Session,
    year: int | None = None,
) -> dict:
    """Aggregate paycheck totals, optionally filtered by year."""
    query = select(PaycheckStub)
    if year:
        start = datetime(year, 1, 1)
        end = datetime(year, 12, 31, 23, 59, 59)
        query = query.where(
            PaycheckStub.pay_date >= start,
            PaycheckStub.pay_date <= end,
        )
    stubs = db.execute(query).scalars().all()

    if not stubs:
        return {
            "count": 0, "total_gross": 0, "total_net": 0,
            "total_taxes": 0, "total_retirement": 0, "total_benefits": 0,
        }

    return {
        "count": len(stubs),
        "total_gross": sum(s.gross_pay for s in stubs),
        "total_net": sum(s.net_pay for s in stubs),
        "total_taxes": sum(s.total_taxes for s in stubs),
        "total_retirement": sum(s.retirement_401k for s in stubs),
        "total_benefits": sum(s.total_benefits for s in stubs),
    }
