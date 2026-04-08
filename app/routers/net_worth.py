from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.net_worth_service import compute_net_worth, compute_net_worth_series

router = APIRouter(prefix="/net-worth", tags=["net_worth"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
def net_worth_page(
    request: Request,
    months: int = Query(12, ge=1, le=120),
    db: Session = Depends(get_db),
):
    current = compute_net_worth(db)
    series = compute_net_worth_series(db, months=months)

    groups: dict[str, float] = {}
    for item in current.breakdown:
        groups.setdefault(item.type_group, 0.0)
        groups[item.type_group] += item.balance

    return templates.TemplateResponse(request, "net_worth/dashboard.html", {
        "current": current,
        "series": series,
        "groups": groups,
        "months": months,
    })


@router.get("/api/data")
def net_worth_api(
    months: int = Query(12, ge=1, le=120),
    db: Session = Depends(get_db),
):
    current = compute_net_worth(db)
    series = compute_net_worth_series(db, months=months)
    return {
        "current": current.model_dump(),
        "series": series.model_dump(),
    }
