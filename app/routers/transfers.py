import json
from datetime import datetime
from app.services.clock import naive_utc_now
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


def _acyclic_edges(pair_flow: dict) -> tuple[list[dict], int]:
    """Turn directed (from,to)→entry flows into an acyclic edge list.

    A Sankey diagram must be a DAG — ``chartjs-chart-sankey`` silently
    renders nothing when the graph contains a cycle. We:

    1. Net every reciprocal pair (A→B and B→A) into one edge in the
       dominant direction (kills all 2-cycles, the common case).
    2. Break any remaining longer cycle by dropping its smallest-flow edge
       until the graph is acyclic.

    Returns ``(edges, dropped_count)``.
    """
    # 1. Net reciprocal pairs.
    netted: list[dict] = []
    processed: set[tuple[str, str]] = set()
    for (f, t), e in pair_flow.items():
        if (f, t) in processed:
            continue
        rev = pair_flow.get((t, f))
        if rev is not None:
            processed.add((f, t))
            processed.add((t, f))
            if e["flow"] >= rev["flow"]:
                src, dst, net = f, t, e["flow"] - rev["flow"]
            else:
                src, dst, net = t, f, rev["flow"] - e["flow"]
            if net <= 0:
                continue  # cancels out exactly — no net movement to draw
            netted.append({
                "from": src, "to": dst, "flow": net,
                "count": e["count"] + rev["count"],
                "native": e["native"] + rev["native"],
                "netted": True,
            })
        else:
            processed.add((f, t))
            netted.append(e)

    # 2. Break any remaining cycle (e.g. A→B→C→A) by removing the smallest
    #    edge until acyclic. Min feedback arc set is NP-hard, but this greedy
    #    pass is fine at the handful-of-accounts scale here.
    def _has_cycle(edges: list[dict]) -> bool:
        from collections import defaultdict
        adj: dict[str, list[str]] = defaultdict(list)
        nodes: set[str] = set()
        for e in edges:
            adj[e["from"]].append(e["to"])
            nodes.add(e["from"])
            nodes.add(e["to"])
        state: dict[str, int] = {}  # 0=visiting, 1=done

        def visit(u: str) -> bool:
            state[u] = 0
            for v in adj[u]:
                if state.get(v) == 0:
                    return True
                if v not in state and visit(v):
                    return True
            state[u] = 1
            return False

        return any(n not in state and visit(n) for n in nodes)

    dropped = 0
    while _has_cycle(netted):
        netted.sort(key=lambda e: e["flow"])
        netted.pop(0)
        dropped += 1

    return netted, dropped


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
    preset: str | None = Query(None),   # 1y, ytd, 2y, mtd, 5y, all
    db: Session = Depends(get_db),
):
    """Sankey diagram of confirmed transfer flows between accounts."""
    from datetime import date, timedelta
    today = date.today()

    # Resolve preset → date_from/date_to (preset takes priority over manual input)
    if preset == "ytd":
        date_from = date(today.year, 1, 1).isoformat()
        date_to = today.isoformat()
    elif preset == "mtd":
        date_from = date(today.year, today.month, 1).isoformat()
        date_to = today.isoformat()
    elif preset == "1y":
        date_from = (today - timedelta(days=365)).isoformat()
        date_to = today.isoformat()
    elif preset == "2y":
        date_from = (today - timedelta(days=730)).isoformat()
        date_to = today.isoformat()
    elif preset == "5y":
        date_from = (today - timedelta(days=1825)).isoformat()
        date_to = today.isoformat()
    elif preset == "all":
        date_from = None
        date_to = None
    elif not date_from and not date_to:
        # Default: 1 year
        preset = "1y"
        date_from = (today - timedelta(days=365)).isoformat()
        date_to = today.isoformat()

    from app.config import settings
    from app.services.fx_service import convert_amount
    base_ccy = settings.base_currency

    FromTxn = Transaction.__table__.alias("from_txn")
    ToTxn = Transaction.__table__.alias("to_txn")
    FromAcct = Account.__table__.alias("from_acct")
    ToAcct = Account.__table__.alias("to_acct")

    # Group by (from_account, to_account, currency) so we can convert each
    # currency bucket to the base currency before summing — mixing GBP and
    # USD into one number was making the Sankey nonsensical with many
    # accounts.
    q = (
        select(
            FromAcct.c.name.label("from_account"),
            ToAcct.c.name.label("to_account"),
            FromAcct.c.currency.label("from_currency"),
            ToAcct.c.currency.label("to_currency"),
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

    q = q.group_by(
        FromAcct.c.name, ToAcct.c.name,
        FromAcct.c.currency, ToAcct.c.currency,
    ).order_by(func.sum(TransferLink.amount).desc())

    rows = db.execute(q).all()

    # Collapse to one entry per (from, to) pair with all flows converted to
    # base currency. Per-currency native totals are kept for tooltips.
    now = naive_utc_now()
    pair_flow: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r.from_account == r.to_account:
            continue
        ccy = (r.from_currency or base_ccy) or base_ccy
        native_amount = float(r.total or 0)
        if ccy == base_ccy:
            base_amount = native_amount
        else:
            conv, _ = convert_amount(
                db, Decimal(str(native_amount)), ccy, base_ccy, now,
            )
            base_amount = float(conv) if conv is not None else native_amount
        key = (r.from_account, r.to_account)
        entry = pair_flow.setdefault(key, {
            "from": r.from_account, "to": r.to_account,
            "flow": 0.0, "count": 0, "native": [],
        })
        entry["flow"] += base_amount
        entry["count"] += int(r.count or 0)
        entry["native"].append({
            "ccy": ccy, "amount": native_amount, "count": int(r.count or 0),
        })

    # ── Make the graph a DAG ────────────────────────────────────────────
    # A Sankey must be acyclic; chartjs-chart-sankey renders *nothing* if it
    # hits a cycle. Reciprocal transfers (A→B and B→A) are the common case,
    # so net each pair into a single dominant-direction edge. Then break any
    # remaining longer cycle by dropping its smallest-flow edge.
    netted_edges, dropped_count = _acyclic_edges(pair_flow)
    sankey_data = sorted(netted_edges, key=lambda d: -d["flow"])

    # Per-account totals for summary table (in base currency)
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
        "preset": preset or "",
        "total_flows": len(sankey_data),
        "node_count": len(accounts),
        "base_currency": base_ccy,
        "dropped_flows": dropped_count,
    })
