from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.transaction import Transaction
from app.services.transfer_detector import (
    detect_transfers,
    link_transfer,
    list_transfer_links,
    unlink_transfer,
)

router = APIRouter(prefix="/transfers", tags=["transfers"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
def transfers_page(request: Request, db: Session = Depends(get_db)):
    candidates = detect_transfers(db)
    linked = list_transfer_links(db)

    linked_details = []
    for link in linked:
        from_txn = db.get(Transaction, link.from_transaction_id)
        to_txn = db.get(Transaction, link.to_transaction_id)
        linked_details.append({
            "link": link,
            "from_txn": from_txn,
            "to_txn": to_txn,
            "from_account": from_txn.account if from_txn else None,
            "to_account": to_txn.account if to_txn else None,
        })

    return templates.TemplateResponse(request, "transfers/review.html", {
        "candidates": candidates,
        "linked": linked_details,
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


@router.post("/unlink/{link_id}")
def remove_link(link_id: int, db: Session = Depends(get_db)):
    unlink_transfer(db, link_id)
    return RedirectResponse(url="/transfers", status_code=303)
