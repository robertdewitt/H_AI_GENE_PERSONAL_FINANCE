"""Multi-part loan payments linked to the single outflow that paid them."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.payment_decomposition import PaymentDecomposition
from app.models.reconciliation import ReconciliationGroup
from app.models.snapshots import LiabilityBalanceSnapshot
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.services.loan_payment_linking import link_loan_payments
from app.services.reconciliation_invariants import validate_group
from app.services.transfer_detector import detect_transfers, list_unmatched_transfers, transfer_partners


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _setup(db):
    checking = Account(name="Mission FCU", account_type=AccountType.CHECKING, currency="USD",
                       is_asset=True, user_id=1)
    loan = Account(name="Villa Mortgage", account_type=AccountType.MORTGAGE, currency="USD",
                   is_asset=False, interest_rate=0.025, user_id=1)
    db.add_all([checking, loan])
    db.flush()
    db.add(LiabilityBalanceSnapshot(
        account_id=loan.id, as_of_date=datetime(2026, 2, 12), value_native=Decimal("205952.96"),
        value_base=Decimal("205952.96"), currency="USD", fx_rate=1.0, source="mortgage_statement_pdf",
        confidence=1.0, stale_flag=False))
    db.flush()
    return checking, loan


def _t(db, acct, d, amount, desc, is_transfer=False):
    t = Transaction(account_id=acct.id, date=datetime.combine(d, datetime.min.time()), description=desc,
                    amount=Decimal(amount), original_currency="USD", is_transfer=is_transfer)
    db.add(t)
    db.flush()
    return t


def _month(db, checking, loan, d, outflow_day=None):
    out = _t(db, checking, outflow_day or d, "-3290.00", "Mission FCU")
    reg = _t(db, loan, d, "3245.24", "RegularPayment", is_transfer=True)
    extra = _t(db, loan, d, "44.76", "PrincipalOnlyPayment", is_transfer=True)
    return out, reg, extra


def test_payment_in_two_parts_is_grouped_and_split(db):
    checking, loan = _setup(db)
    out, reg, extra = _month(db, checking, loan, date(2026, 3, 3), outflow_day=date(2026, 3, 2))

    result = link_loan_payments(db, loan)
    assert len(result.linked) == 1 and not result.unmatched

    group = db.execute(select(ReconciliationGroup)).scalar_one()
    assert validate_group(db, group).balanced

    splits = {s.event_type: s for s in db.execute(
        select(TransactionSplit).where(TransactionSplit.transaction_id == out.id)).scalars()}
    # Interest from the February statement: 205,952.96 × 2.5% / 12.
    assert splits["mortgage_interest"].amount_native == Decimal("-429.07")
    assert splits["mortgage_interest"].counts_as_true_spend is True
    principal = [s for s in db.execute(select(TransactionSplit).where(
        TransactionSplit.transaction_id == out.id, TransactionSplit.event_type == "mortgage_principal")).scalars()]
    assert sorted(s.amount_native for s in principal) == [Decimal("-2816.17"), Decimal("-44.76")]
    assert all(s.linked_account_id == loan.id for s in principal)

    comps = {(d.transaction_id, d.component): d.amount for d in db.execute(select(PaymentDecomposition)).scalars()}
    assert comps[(reg.id, "interest")] == Decimal("429.07")
    assert comps[(reg.id, "principal")] == Decimal("2816.17")
    assert comps[(extra.id, "principal")] == Decimal("44.76")


def test_interest_rolls_forward_between_statements(db):
    checking, loan = _setup(db)
    _month(db, checking, loan, date(2026, 3, 3))
    out, *_ = _month(db, checking, loan, date(2026, 4, 3))
    link_loan_payments(db, loan)

    april = db.execute(select(TransactionSplit).where(
        TransactionSplit.transaction_id == out.id, TransactionSplit.event_type == "mortgage_interest")).scalar_one()
    # 205,952.96 + 429.07 − 3,290 = 203,092.03 → × 2.5% / 12
    assert april.amount_native == Decimal("-423.11")


def test_retried_payment_day_matches_only_the_attempt_that_was_funded(db):
    checking, loan = _setup(db)
    _t(db, checking, date(2026, 3, 10), "-3290.00", "Mission FCU")
    for _ in range(2):                                   # same parts posted twice on 8 Mar
        _t(db, loan, date(2026, 3, 8), "3245.24", "Payments Irregular", is_transfer=True)
        _t(db, loan, date(2026, 3, 8), "44.76", "Payments Principal", is_transfer=True)

    result = link_loan_payments(db, loan)
    assert len(result.linked) == 1 and len(result.unmatched) == 1


def test_ambiguous_outflows_are_left_alone(db):
    checking, loan = _setup(db)
    _month(db, checking, loan, date(2026, 3, 3))
    _t(db, checking, date(2026, 3, 4), "-3290.00", "Something else")

    result = link_loan_payments(db, loan)
    assert not result.linked and len(result.unmatched) == 1


def test_linking_is_idempotent(db):
    checking, loan = _setup(db)
    _month(db, checking, loan, date(2026, 3, 3))
    link_loan_payments(db, loan)
    assert link_loan_payments(db, loan).linked == []
    assert len(db.execute(select(ReconciliationGroup)).scalars().all()) == 1


def test_grouped_rows_show_partner_and_leave_the_transfer_queues(db):
    checking, loan = _setup(db)
    out, reg, extra = _month(db, checking, loan, date(2026, 3, 3))
    link_loan_payments(db, loan)

    partners = transfer_partners(db, [out, reg, extra], {checking.id, loan.id})
    assert partners[out.id].id == loan.id
    assert partners[reg.id].id == checking.id and partners[extra.id].id == checking.id
    assert not any(u["txn"].id in (reg.id, extra.id) for u in list_unmatched_transfers(db))
    grouped = {out.id, reg.id, extra.id}
    assert not any({c.from_transaction_id, c.to_transaction_id} & grouped for c in detect_transfers(db))
