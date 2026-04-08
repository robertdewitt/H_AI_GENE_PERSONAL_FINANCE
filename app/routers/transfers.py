from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.transaction import Transaction
from app.services.transfer_detector import (
    detect_transfers,
    link_transfer,
    list_transfer_links,
    list_unmatched_transfers,
    scan_and_flag_payments,
    unlink_transfer,
)

router = APIRouter(prefix="/transfers", tags=["transfers"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


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


@router.post("/unlink/{link_id}")
def remove_link(link_id: int, db: Session = Depends(get_db)):
    unlink_transfer(db, link_id)
    return RedirectResponse(url="/transfers", status_code=303)
