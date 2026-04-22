import json
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.transfer_link import TransferLink
from app.services.transfer_detector import (
    detect_transfers,
    dismiss_transfer_pair,
    link_transfer,
    list_transfer_links,
    list_unmatched_transfers,
    scan_and_flag_payments,
    unlink_transfer,
)

router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.get("", response_class=HTMLResponse)
def transfers_page(
    request: Request,
    scanned: int | None = Query(None),
    linked: int | None = Query(None, alias="linked_count"),
    db: Session = Depends(get_db),
):
    candidates = detect_transfers(db)
    confirmed = list_transfer_links(db)
    unmatched = list_unmatched_transfers(db)

    confirmed_details = []
    for link in confirmed:
        from_txn = db.get(Transaction, link.from_transaction_id)
        to_txn = db.get(Transaction, link.to_transaction_id)
        confirmed_details.append({
            "link": link,
            "from_txn": from_txn,
            "to_txn": to_txn,
            "from_account": from_txn.account if from_txn else None,
            "to_account": to_txn.account if to_txn else None,
        })

    return templates.TemplateResponse(request, "transfers/review.html", {
        "candidates": candidates,
        "linked": confirmed_details,
        "unmatched": unmatched,
        "scanned": scanned,
        "linked_count": linked,
    })


@router.post("/link")
def create_link(
    from_transaction_id: int = Form(...),
    to_transaction_id: int = Form(...),
    confidence: float = Form(1.0),
    db: Session = Depends(get_db),
):
    link_transfer(
        db,
        from_transaction_id,
        to_transaction_id,
        confirmed=True,
        confidence=confidence,
    )
    return RedirectResponse(url="/transfers", status_code=303)


@router.post("/scan-payments")
def scan_payments(db: Session = Depends(get_db)):
    """Scan liability accounts for payment-like transactions and flag them."""
    count = scan_and_flag_payments(db)
    return RedirectResponse(
        url=f"/transfers?scanned={count}", status_code=303,
    )


@router.post("/bulk-link")
def bulk_link(
    min_confidence: float = Form(0.7),
    db: Session = Depends(get_db),
):
    """Confirm all transfer candidates at or above the given confidence."""
    candidates = detect_transfers(db)
    linked_count = 0
    for c in candidates:
        if c.confidence >= min_confidence:
            result = link_transfer(
                db,
                c.from_transaction_id,
                c.to_transaction_id,
                confirmed=True,
                confidence=c.confidence,
            )
            if result:
                linked_count += 1
    return RedirectResponse(
        url=f"/transfers?linked_count={linked_count}", status_code=303,
    )


@router.post("/dismiss")
def dismiss_pair(
    from_transaction_id: int = Form(...),
    to_transaction_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Decline a transfer candidate — marks both transactions as not transfers."""
    dismiss_transfer_pair(db, from_transaction_id, to_transaction_id)
    return RedirectResponse(url="/transfers", status_code=303)


@router.post("/unlink/{link_id}")
def remove_link(link_id: int, db: Session = Depends(get_db)):
    unlink_transfer(db, link_id)
    return RedirectResponse(url="/transfers", status_code=303)


@router.get("/flow", response_class=HTMLResponse)
def transfer_flow(
    request: Request,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Sankey diagram of confirmed transfer flows between accounts."""
    FromTxn = Transaction.__table__.alias("from_txn")
    ToTxn = Transaction.__table__.alias("to_txn")
    FromAcct = Account.__table__.alias("from_acct")
    ToAcct = Account.__table__.alias("to_acct")

    q = (
        select(
            FromAcct.c.name.label("from_account"),
            ToAcct.c.name.label("to_account"),
            func.sum(TransferLink.amount).label("total"),
            func.count(TransferLink.id).label("count"),
        )
        .join(FromTxn, TransferLink.from_transaction_id == FromTxn.c.id)
        .join(ToTxn, TransferLink.to_transaction_id == ToTxn.c.id)
        .join(FromAcct, FromTxn.c.account_id == FromAcct.c.id)
        .join(ToAcct, ToTxn.c.account_id == ToAcct.c.id)
        .where(TransferLink.confirmed_by_user == True)
    )

    df_from = date_from
    df_to = date_to
    if df_from:
        try:
            q = q.where(TransferLink.date >= datetime.strptime(df_from, "%Y-%m-%d"))
        except ValueError:
            df_from = None
    if df_to:
        try:
            q = q.where(TransferLink.date <= datetime.strptime(df_to, "%Y-%m-%d"))
        except ValueError:
            df_to = None

    q = q.group_by(FromAcct.c.name, ToAcct.c.name).order_by(func.sum(TransferLink.amount).desc())

    rows = db.execute(q).all()

    # Build Sankey data: [{from, to, flow}]
    sankey_data = [
        {
            "from": r.from_account,
            "to": r.to_account,
            "flow": float(r.total),
            "count": r.count,
        }
        for r in rows
        if r.from_account != r.to_account  # skip self-transfers
    ]

    # Per-account totals for summary table
    out_totals: dict[str, float] = {}
    in_totals: dict[str, float] = {}
    for r in sankey_data:
        out_totals[r["from"]] = out_totals.get(r["from"], 0) + r["flow"]
        in_totals[r["to"]] = in_totals.get(r["to"], 0) + r["flow"]

    accounts = sorted(set(list(out_totals) + list(in_totals)))
    summary = [
        {
            "account": a,
            "out": out_totals.get(a, 0),
            "in": in_totals.get(a, 0),
            "net": in_totals.get(a, 0) - out_totals.get(a, 0),
        }
        for a in accounts
    ]

    return templates.TemplateResponse(request, "transfers/flow.html", {
        "sankey_data": json.dumps(sankey_data),
        "summary": summary,
        "date_from": df_from or "",
        "date_to": df_to or "",
        "total_flows": len(sankey_data),
    })
