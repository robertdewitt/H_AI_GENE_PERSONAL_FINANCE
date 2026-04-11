"""Apply parsed financial documents to the ledger: documents, lines, txn, splits.

Also writes property P&L snapshots for rental statements (time series).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    ClassificationProvenance,
    DocumentLineKind,
    DocumentType,
    EconomicEventType,
    SnapshotSource,
    SpendType,
)
from app.models.financial_document import (
    FinancialDocument,
    FinancialDocumentLine,
    PropertyPnLSnapshot,
)
from app.models.rental_property import RentalProperty
from app.models.transaction import Transaction
from app.services.document_parse import ParsedFinancialDocument, ParsedLine
from app.services.split_service import add_split, validate_splits


@dataclass
class ApplyDocumentResult:
    document_id: int
    transaction_id: int
    split_validation_ok: bool
    property_pnl_snapshot_id: int | None = None
    warnings: list[str] | None = None


def _event_for_payroll_line(line: ParsedLine) -> EconomicEventType:
    code = (line.code or "").lower()
    if line.excluded_from_net_sum and line.amount > 0:
        return EconomicEventType.EMPLOYER_BENEFIT
    if code in ("salary_gross", "gross", "gross_pay"):
        return EconomicEventType.PAYROLL_INCOME
    if "tax" in code or code in ("federal_tax", "state_tax", "local_tax", "social_security", "medicare", "fica"):
        return EconomicEventType.TAX_PAYMENT
    if "401" in code or "pension" in code or code in ("retirement_401k", "401k_employee"):
        return EconomicEventType.INVESTMENT_CONTRIBUTION
    if code in ("health_insurance", "dental", "vision", "hsa", "health"):
        return EconomicEventType.FEE
    if code in ("net_pay", "net_salary", "net_salary_cash"):
        return EconomicEventType.EXTERNAL_INCOME
    if line.kind == DocumentLineKind.INCOME.value:
        return EconomicEventType.PAYROLL_INCOME
    if line.kind == DocumentLineKind.EXPENSE.value:
        return EconomicEventType.TAX_PAYMENT if "tax" in line.label.lower() else EconomicEventType.FEE
    return EconomicEventType.PAYROLL_INCOME


def _spend_for_payroll_line(event: EconomicEventType) -> SpendType | None:
    if event == EconomicEventType.TAX_PAYMENT:
        return SpendType.TAX
    if event in (EconomicEventType.FEE,):
        return SpendType.FIXED_CORE
    return None


def _split_amount_for_line(line: ParsedLine) -> float:
    """Cash/economic amount on the parent transaction's currency."""
    if line.excluded_from_net_sum:
        return 0.0
    return line.amount


def _event_for_rental_line(line: ParsedLine) -> EconomicEventType:
    code = (line.code or "").lower()
    kind = line.kind
    if kind == DocumentLineKind.INCOME.value or code in ("rent", "rental_income", "late_fee_income"):
        return EconomicEventType.RENTAL_INCOME
    if kind == DocumentLineKind.EXPENSE.value:
        return EconomicEventType.RENTAL_EXPENSE
    if kind == DocumentLineKind.TRANSFER.value or code in ("owner_draw", "distribution"):
        return EconomicEventType.OWNER_DISTRIBUTION
    if kind == DocumentLineKind.LIABILITY.value or "prepaid" in code:
        return EconomicEventType.DEFERRED_RENT_LIABILITY
    return EconomicEventType.RENTAL_EXPENSE


def get_or_create_property_by_code(
    db: Session, code: str, name: str | None = None,
) -> RentalProperty:
    row = db.execute(
        select(RentalProperty).where(RentalProperty.code == code).limit(1),
    ).scalar_one_or_none()
    if row:
        return row
    prop = RentalProperty(
        code=code,
        name=name or code.replace("_", " ").title(),
    )
    db.add(prop)
    db.flush()
    return prop


def apply_financial_document(
    db: Session,
    parsed: ParsedFinancialDocument,
    account_id: int,
    *,
    rental_property_id: int | None = None,
    pension_account_id: int | None = None,
    provenance: str = ClassificationProvenance.IMPORTED.value,
    confidence: float = 0.95,
) -> ApplyDocumentResult:
    """Persist document + lines, create parent txn and splits, optional P&L snapshot."""
    warnings: list[str] = []

    if parsed.document_type == DocumentType.RENTAL_STATEMENT.value:
        if not parsed.property_code:
            raise ValueError("rental_statement requires property_code")
        prop = get_or_create_property_by_code(
            db, parsed.property_code, name=parsed.property_code,
        )
        rental_property_id = prop.id

    doc_type_str = (
        DocumentType.PAYROLL.value
        if parsed.document_type == "payroll"
        else DocumentType.RENTAL_STATEMENT.value
    )

    doc = FinancialDocument(
        document_type=doc_type_str,
        account_id=account_id,
        rental_property_id=rental_property_id,
        statement_date=parsed.statement_date,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        currency=parsed.currency,
        reference=parsed.reference,
        employer_or_counterparty=parsed.employer_or_counterparty,
        raw_payload_json=json.dumps(parsed.raw, default=str),
        provenance=provenance,
        confidence=confidence,
    )
    db.add(doc)
    db.flush()

    line_rows: list[FinancialDocumentLine] = []
    for i, ln in enumerate(parsed.lines):
        row = FinancialDocumentLine(
            document_id=doc.id,
            line_order=i,
            line_kind=ln.kind,
            component_code=ln.code,
            label=ln.label,
            amount_native=ln.amount,
            currency=ln.currency,
            is_cash=ln.is_cash,
            rental_property_id=rental_property_id,
            extra_json=json.dumps(ln.extra) if ln.extra else None,
        )
        db.add(row)
        line_rows.append(row)
    db.flush()

    if parsed.document_type == "payroll":
        net = parsed.net_pay
        assert net is not None
        desc = f"Payroll {parsed.employer_or_counterparty or 'deposit'} {parsed.statement_date.date()}"
        event_parent = EconomicEventType.PAYROLL_INCOME.value
    else:
        net = parsed.net_bank_deposit
        assert net is not None
        desc = (
            f"Rental operating {parsed.property_code} "
            f"{parsed.statement_date.date()}"
        )
        event_parent = EconomicEventType.RENTAL_INCOME.value

    txn = Transaction(
        account_id=account_id,
        date=parsed.statement_date,
        description=desc[:500],
        amount=net,
        original_currency=parsed.currency,
        event_type=event_parent,
        classification_provenance=provenance,
        classification_confidence=confidence,
        financial_document_id=doc.id,
    )
    db.add(txn)
    db.flush()

    for pr_line, fd_line in zip(parsed.lines, line_rows):
        if parsed.document_type == "payroll":
            ev = _event_for_payroll_line(pr_line)
            st = _spend_for_payroll_line(ev)
            sa = _split_amount_for_line(pr_line)
            linked = pension_account_id if pr_line.code and (
                "401" in (pr_line.code or "").lower()
                or "pension" in (pr_line.code or "").lower()
            ) else None
            counts_spend = bool(st)
        else:
            ev = _event_for_rental_line(pr_line)
            st = None
            if ev == EconomicEventType.RENTAL_EXPENSE:
                st = SpendType.FIXED_CORE
                counts_spend = True
            else:
                counts_spend = False
            sa = _split_amount_for_line(pr_line)
            linked = None
            if pr_line.kind == DocumentLineKind.LIABILITY.value and not pr_line.is_cash:
                sa = 0.0

        add_split(
            db,
            transaction_id=txn.id,
            amount_native=sa,
            currency=parsed.currency,
            event_type=ev,
            spend_type=st,
            counts_as_true_spend=counts_spend,
            linked_account_id=linked,
            document_line_id=fd_line.id,
            provenance=provenance,
            confidence=confidence,
            notes=pr_line.code or pr_line.label,
            as_of_date=parsed.statement_date,
        )

    val = validate_splits(db, txn.id)
    doc.split_validation_ok = val.valid
    if not val.valid:
        warnings.extend(val.warnings)

    pnl_id = None
    if parsed.document_type == "rental_statement" and rental_property_id:
        pnl_id = _write_property_pnl_snapshot(
            db, doc.id, rental_property_id, parsed, confidence,
        )

    db.flush()
    return ApplyDocumentResult(
        document_id=doc.id,
        transaction_id=txn.id,
        split_validation_ok=val.valid,
        property_pnl_snapshot_id=pnl_id,
        warnings=warnings or None,
    )


def _write_property_pnl_snapshot(
    db: Session,
    document_id: int,
    property_id: int,
    parsed: ParsedFinancialDocument,
    confidence: float,
) -> int:
    inc = 0.0
    exp = 0.0
    draw = 0.0
    liab = 0.0
    for ln in parsed.lines:
        k = ln.kind
        if k == DocumentLineKind.INCOME.value:
            inc += max(0.0, ln.amount)
        elif k == DocumentLineKind.EXPENSE.value:
            exp += abs(min(0.0, ln.amount))
        elif k == DocumentLineKind.TRANSFER.value:
            draw += abs(min(0.0, ln.amount))
        elif k == DocumentLineKind.LIABILITY.value:
            liab += ln.amount

    noi = inc - exp
    cash = parsed.net_bank_deposit or 0.0

    snap = PropertyPnLSnapshot(
        rental_property_id=property_id,
        financial_document_id=document_id,
        period_start=parsed.period_start or parsed.statement_date,
        period_end=parsed.period_end or parsed.statement_date,
        statement_date=parsed.statement_date,
        currency=parsed.currency,
        total_income=inc,
        total_expense=exp,
        owner_draw=draw,
        liability_adjustment=liab,
        net_operating_income=noi,
        net_cash_flow=cash,
        source=SnapshotSource.IMPORTED.value,
        confidence=confidence,
        stale_flag=False,
    )
    db.add(snap)
    db.flush()
    return snap.id


def list_payroll_documents(
    db: Session,
    limit: int = 120,
) -> list[FinancialDocument]:
    return db.execute(
        select(FinancialDocument)
        .where(FinancialDocument.document_type == DocumentType.PAYROLL.value)
        .order_by(FinancialDocument.statement_date.desc())
        .limit(limit),
    ).scalars().all()


def list_property_pnl_series(
    db: Session,
    rental_property_id: int,
    limit: int = 120,
) -> list[PropertyPnLSnapshot]:
    return db.execute(
        select(PropertyPnLSnapshot)
        .where(PropertyPnLSnapshot.rental_property_id == rental_property_id)
        .order_by(PropertyPnLSnapshot.statement_date.desc())
        .limit(limit),
    ).scalars().all()
