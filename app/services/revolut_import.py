"""Import a parsed Revolut PDF statement through the standard import path.

The Revolut confirm route used to insert rows itself with an exact-key
duplicate check only. That bypassed everything the CSV path learned: the
source fingerprint, near-duplicate wording, the memory of rows the user
deleted, reconciliation of user-entered expectations with the bank's row,
and handing the running balance to rows already on the ledger. The parsed
rows are written to a small CSV beside the PDF and imported like any other
file, so a Revolut statement gets the same treatment as a CSV export.
"""
from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.import_batch import ImportBatch, ImportSource
from app.services.revolut_pdf_parser import parse_revolut_pdf, revolut_statement_currency

MAPPING = {"date": "Date", "description": "Description", "amount": "Amount", "balance": "Balance"}


def import_revolut_statement(
    db: Session,
    account_id: int,
    pdf_path: str,
    include_sections: set[str] | None = None,
    account_currency: str = "GBP",
) -> ImportBatch:
    """Parse the chosen sections and import them; returns the batch."""
    from app.services.import_service import import_transactions

    txns = parse_revolut_pdf(pdf_path, include_sections=include_sections or {"main"})
    currency = revolut_statement_currency(pdf_path) or account_currency

    pdf = Path(pdf_path)
    csv_path = pdf.with_name(pdf.stem + ".rows.csv")
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Date", "Description", "Amount", "Balance", "Section"])
        for t in txns:
            w.writerow([t["date"].strftime("%Y-%m-%d"), t["description"], str(t["amount"]),
                        "" if t["balance"] is None else str(t["balance"]), t.get("section", "")])
    try:
        batch = import_transactions(
            db, account_id, str(csv_path), column_mapping=MAPPING,
            account_currency=currency, is_liability=False, dayfirst=False,
        )
    finally:
        try:
            csv_path.unlink()
        except OSError:
            pass
    batch.filename = pdf.name
    batch.source = ImportSource.REVOLUT_PDF.value
    db.commit()
    return batch
