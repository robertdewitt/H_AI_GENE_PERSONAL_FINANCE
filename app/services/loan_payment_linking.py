"""Link loan payments that arrive in parts to the single payment that funded them.

A mortgage servicer often posts one payment as several rows — the regular
payment and an extra-principal payment — while the paying account shows one
outflow for the total. A pairwise TransferLink can't express that, so the
parts and the outflow become one ReconciliationGroup (loan_payment) whose
members net to zero.

The paying side is then split so the ledger says what the money did:

* interest      — Interest Expense, real spending (mortgage_interest)
* principal     — moved to the loan (mortgage_principal), linked to it
* extra principal

and each loan-side part gets a PaymentDecomposition (interest / principal),
which the loan's Payment Breakdown and interest-YTD figures read.

Interest per payment comes from the outstanding balance before it:
* the latest statement balance since the previous payment, when there is one
  (exact — statements here move by payment − balance × rate / 12);
* otherwise the previous balance rolled forward by amortisation;
* before the first statement, worked back from it (marked estimated).

``link_loan_payments`` is idempotent: parts already in a group are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.account import Account, AccountType
from app.models.enums import (
    ClassificationProvenance,
    EconomicEventType,
    PaymentComponent,
    ReconciliationGroupType,
    ReconciliationStatus,
)
from app.models.payment_decomposition import PaymentDecomposition
from app.models.reconciliation import ReconciliationGroup, ReconciliationMember
from app.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOAN_TYPES = (AccountType.MORTGAGE, AccountType.LOAN)
DATE_WINDOW = timedelta(days=5)       # outflow vs posting date
CENT = Decimal("0.01")


@dataclass
class LinkResult:
    linked: list[tuple[date, Decimal]] = field(default_factory=list)       # (date, total)
    unmatched: list[tuple[date, list[Decimal]]] = field(default_factory=list)


def _d(dt) -> date:
    return dt.date() if hasattr(dt, "date") else dt


def grouped_transaction_ids(db: "Session") -> set[int]:
    return {tid for (tid,) in db.execute(select(ReconciliationMember.transaction_id)).all()}


def _category_id(db: "Session", user_id: int | None, name: str) -> int | None:
    from app.models.category import Category
    q = select(Category.id).where(Category.name == name)
    if user_id is not None:
        q = q.where(Category.user_id == user_id)
    return db.execute(q.limit(1)).scalar_one_or_none()


def _payment_sets(db: "Session", loan: Account, taken: set[int]) -> list[list[Transaction]]:
    """Ungrouped incoming payment rows on the loan, one list per posting date,
    largest first. Several rows the same day are one payment in parts."""
    rows = db.execute(
        select(Transaction).where(
            Transaction.account_id == loan.id,
            Transaction.amount > 0,
            Transaction.is_transfer.is_(True),
        ).order_by(Transaction.date, Transaction.amount.desc())
    ).scalars().all()
    by_day: dict[date, list[Transaction]] = {}
    for t in rows:
        if t.id not in taken and t.transfer_link_id is None:
            by_day.setdefault(_d(t.date), []).append(t)
    sets = []
    for day in sorted(by_day):
        rows = by_day[day]
        # A retried payment posts the same parts more than once in a day
        # (3245.24 + 44.76, three times): split into one set per attempt.
        amounts = [Decimal(t.amount) for t in rows]
        k = amounts.count(amounts[0])
        if k > 1 and all(amounts.count(a) == k for a in set(amounts)):
            for i in range(k):
                sets.append(rows[i::k])
        else:
            sets.append(rows)
    return sets


def _funding_outflow(db: "Session", loan: Account, parts: list[Transaction],
                     taken: set[int]) -> Transaction | None:
    """The one outflow on another of the owner's accounts that paid for these
    parts: same total, within a few days, not already used. None when there
    isn't exactly one candidate — guessing would misattribute history."""
    total = sum(Decimal(p.amount) for p in parts)
    day = _d(parts[0].date)
    owned = select(Account.id).where(Account.user_id == loan.user_id, Account.id != loan.id)
    candidates = [
        t for t in db.execute(
            select(Transaction).where(
                Transaction.account_id.in_(owned),
                Transaction.amount == -total,
                Transaction.date >= day - DATE_WINDOW,
                Transaction.date <= day + DATE_WINDOW + timedelta(days=1),
                Transaction.transfer_link_id.is_(None),
            )
        ).scalars()
        if t.id not in taken
    ]
    return candidates[0] if len(candidates) == 1 else None


def _interest_schedule(db: "Session", loan: Account,
                       payments: list[tuple[date, Decimal]]) -> dict[date, tuple[Decimal, bool]]:
    """Interest for each (date, total paid), and whether it's estimated."""
    from app.models.snapshots import LiabilityBalanceSnapshot

    rate = Decimal(str(loan.interest_rate or 0)) / 12
    if not rate or not payments:
        return {}
    snaps = sorted(
        (_d(s.as_of_date), Decimal(s.value_native))
        for s in db.execute(
            select(LiabilityBalanceSnapshot).where(LiabilityBalanceSnapshot.account_id == loan.id)
        ).scalars()
    )
    out: dict[date, tuple[Decimal, bool]] = {}
    if not snaps:
        return out
    payments = sorted(payments)
    first_snap_day, first_snap_bal = snaps[0]

    # Before the first statement: work back from it.
    before = [p for p in payments if p[0] < first_snap_day]
    after_bal = first_snap_bal
    for day, paid in reversed(before):
        balance_before = (after_bal + paid) / (1 + rate)
        out[day] = ((balance_before * rate).quantize(CENT, ROUND_HALF_UP), True)
        after_bal = balance_before

    # From the first statement on: anchor to statements, roll forward between.
    balance, prev_day = None, None
    for day, paid in payments:
        if day < first_snap_day:
            continue
        stmts = [b for s, b in snaps if s < day and (prev_day is None or s >= prev_day)]
        if stmts:
            balance = stmts[-1]
        if balance is None:
            continue
        interest = (balance * rate).quantize(CENT, ROUND_HALF_UP)
        out[day] = (interest, not stmts)
        balance = balance + interest - paid
        prev_day = day
    return out


def _link(db: "Session", loan: Account, outflow: Transaction, parts: list[Transaction],
          interest: Decimal | None, estimated: bool) -> ReconciliationGroup:
    from app.services.split_service import add_split, delete_splits_for_transaction

    total = sum(Decimal(p.amount) for p in parts)
    group = ReconciliationGroup(
        group_type=ReconciliationGroupType.LOAN_PAYMENT.value,
        status=ReconciliationStatus.MATCHED.value,
        base_currency=loan.currency or "USD",
        residual_base=Decimal(outflow.amount) + total,
        confidence=0.95,
        provenance=ClassificationProvenance.INFERRED.value,
        as_of_date=outflow.date,
        notes=f"{loan.name} payment in {len(parts)} parts",
    )
    db.add(group)
    db.flush()
    for t, role in [(outflow, "source")] + [(p, "destination") for p in parts]:
        db.add(ReconciliationMember(
            group_id=group.id, transaction_id=t.id, role=role,
            allocated_amount_native=Decimal(t.amount), allocated_currency=t.original_currency or loan.currency,
            allocated_amount_base=Decimal(t.amount),
        ))

    # Paying side: interest is spending, principal moves to the loan.
    regular, extras = parts[0], parts[1:]
    principal_regular = Decimal(regular.amount) - (interest or 0)
    ccy = outflow.original_currency or loan.currency
    delete_splits_for_transaction(db, outflow.id)
    lines = []
    if interest:
        lines.append(dict(amount=-interest, category_id=_category_id(db, loan.user_id, "Interest Expense"),
                          event_type=EconomicEventType.MORTGAGE_INTEREST.value, linked_account_id=None,
                          notes=f"{loan.name} interest" + (" (estimated)" if estimated else "")))
    lines.append(dict(amount=-principal_regular, category_id=_category_id(db, loan.user_id, "Account Transfer"),
                      event_type=EconomicEventType.MORTGAGE_PRINCIPAL.value, linked_account_id=loan.id,
                      notes=f"{loan.name} principal"))
    for e in extras:
        lines.append(dict(amount=-Decimal(e.amount), category_id=_category_id(db, loan.user_id, "Account Transfer"),
                          event_type=EconomicEventType.MORTGAGE_PRINCIPAL.value, linked_account_id=loan.id,
                          notes=f"{loan.name} extra principal"))
    for line in lines:
        add_split(db, transaction_id=outflow.id, amount_native=line["amount"], currency=ccy,
                  category_id=line["category_id"], event_type=line["event_type"],
                  linked_account_id=line["linked_account_id"], linked_reconciliation_group_id=group.id,
                  provenance=ClassificationProvenance.INFERRED.value, confidence=0.9 if estimated else 0.99,
                  notes=line["notes"], as_of_date=outflow.date)
    outflow.event_type = EconomicEventType.MORTGAGE_PAYMENT.value

    # Loan side: what each part paid.
    prov = ClassificationProvenance.INFERRED.value
    for t, comps in [(regular, [(PaymentComponent.INTEREST, interest), (PaymentComponent.PRINCIPAL, principal_regular)])] + \
                    [(e, [(PaymentComponent.PRINCIPAL, Decimal(e.amount))]) for e in extras]:
        for comp, amt in comps:
            if amt:
                db.add(PaymentDecomposition(
                    transaction_id=t.id, component=comp.value, amount=amt,
                    currency=t.original_currency or loan.currency, amount_base=amt,
                    provenance=prov, confidence=0.9 if estimated else 0.99,
                    notes="estimated from the first statement" if estimated and comp == PaymentComponent.INTEREST else None,
                ))
    db.flush()
    return group


def link_loan_payments(db: "Session", loan: Account) -> LinkResult:
    """Group every ungrouped multi-part payment on ``loan`` with the outflow
    that funded it, and split out interest and principal."""
    result = LinkResult()
    if loan.account_type not in LOAN_TYPES:
        return result
    taken = grouped_transaction_ids(db)
    matches = []
    for parts in _payment_sets(db, loan, taken):
        if len(parts) < 2:
            continue                       # single-row payments go through TransferLink
        outflow = _funding_outflow(db, loan, parts, taken)
        if outflow is None:
            result.unmatched.append((_d(parts[0].date), [Decimal(p.amount) for p in parts]))
            continue
        taken.update({outflow.id, *(p.id for p in parts)})
        matches.append((outflow, parts))

    interest = _interest_schedule(
        db, loan, [(_d(p[0].date), sum(Decimal(x.amount) for x in p)) for _, p in matches])
    for outflow, parts in matches:
        i, est = interest.get(_d(parts[0].date), (None, False))
        _link(db, loan, outflow, parts, i, est)
        result.linked.append((_d(parts[0].date), sum(Decimal(p.amount) for p in parts)))
    return result


def link_all_loan_payments(db: "Session", user_id: int | None = None) -> dict[int, LinkResult]:
    q = select(Account).where(Account.account_type.in_(LOAN_TYPES), Account.closed_at.is_(None))
    if user_id is not None:
        q = q.where(Account.user_id == user_id)
    return {a.id: link_loan_payments(db, a) for a in db.execute(q).scalars().all()}
