"""Structured multi-line documents: payroll payslips and rental statements."""
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.enums import DocumentType
from app.models.transaction import Transaction
from app.services.document_apply import (
    apply_financial_document,
    list_payroll_documents,
    list_property_pnl_series,
)
from app.services.document_parse import parse_document_dict, parse_document_json


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "documents"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _checking(db):
    acct = Account(
        name="Checking",
        account_type=AccountType.CHECKING,
        currency="USD",
        is_asset=True,
    )
    db.add(acct)
    db.flush()
    return acct


def _rental_op(db):
    acct = Account(
        name="Oak St Operating",
        account_type=AccountType.CHECKING,
        currency="USD",
        is_asset=True,
    )
    db.add(acct)
    db.flush()
    return acct


class TestParseDocuments:
    def test_parse_payroll_sample_file(self):
        parsed = parse_document_json(FIXTURES / "payroll_payslip_sample.json")
        assert parsed.document_type == "payroll"
        assert parsed.net_pay == Decimal("5238.72")
        assert len(parsed.lines) == 8
        excluded = [ln for ln in parsed.lines if ln.excluded_from_net_sum]
        assert len(excluded) == 1

    def test_parse_rental_sample_file(self):
        parsed = parse_document_json(FIXTURES / "rental_statement_sample.json")
        assert parsed.document_type == "rental_statement"
        assert parsed.net_bank_deposit == Decimal("310.00")
        assert parsed.property_code == "oak_st_duplex"

    def test_payroll_sum_mismatch_raises(self):
        data = {
            "document_type": "payroll",
            "currency": "USD",
            "pay_date": "2025-05-15",
            "net_pay": 100.0,
            "lines": [
                {"kind": "income", "code": "salary_gross", "label": "Gross", "amount": 50.0},
            ],
        }
        with pytest.raises(ValueError, match="does not match net_pay"):
            parse_document_dict(data)


class TestApplyPayrollDocument:
    def test_apply_sample_creates_splits(self, db):
        acct = _checking(db)
        parsed = parse_document_json(FIXTURES / "payroll_payslip_sample.json")
        result = apply_financial_document(db, parsed, acct.id)
        db.commit()

        assert result.split_validation_ok is True
        txn = db.get(Transaction, result.transaction_id)
        assert txn.amount == Decimal("5238.72")
        assert txn.financial_document_id == result.document_id
        assert len(txn.splits) == 8
        assert all(s.document_line_id is not None for s in txn.splits)
        non_cash_split = [s for s in txn.splits if s.amount_native == Decimal("0.00")]
        assert len(non_cash_split) == 1

        docs = list_payroll_documents(db)
        assert len(docs) == 1
        assert docs[0].document_type == DocumentType.PAYROLL.value


class TestApplyRentalDocument:
    def test_apply_sample_pnl_snapshot(self, db):
        acct = _rental_op(db)
        parsed = parse_document_json(FIXTURES / "rental_statement_sample.json")
        result = apply_financial_document(db, parsed, acct.id)
        db.commit()

        assert result.property_pnl_snapshot_id is not None
        assert result.split_validation_ok is True

        from app.models.financial_document import PropertyPnLSnapshot

        snap = db.get(PropertyPnLSnapshot, result.property_pnl_snapshot_id)
        assert snap.total_income == Decimal("2400.00")
        assert snap.total_expense == Decimal("590.00")
        assert snap.owner_draw == Decimal("1500.00")
        assert snap.liability_adjustment == Decimal("600.00")
        assert snap.net_operating_income == Decimal("1810.00")
        assert snap.net_cash_flow == Decimal("310.00")

        series = list_property_pnl_series(db, snap.rental_property_id)
        assert len(series) == 1
