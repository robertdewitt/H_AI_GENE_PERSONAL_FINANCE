from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, init_db
from app.models.account import Account
from app.models.transaction import Transaction
from app.routers import (
    accounts, transactions, imports, transfers, net_worth,
    paychecks, valuations, fx, categories,
)
from app.services.net_worth_service import compute_net_worth, compute_net_worth_series


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _seed_categories()
    yield


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(templates_dir))

app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(imports.router)
app.include_router(transfers.router)
app.include_router(net_worth.router)
app.include_router(paychecks.router)
app.include_router(valuations.router)
app.include_router(fx.router)
app.include_router(categories.router)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    nw = compute_net_worth(db)
    account_count = db.execute(select(func.count(Account.id))).scalar() or 0

    recent = db.execute(
        select(Transaction).order_by(Transaction.date.desc()).limit(10)
    ).scalars().all()

    series = compute_net_worth_series(db, months=12)
    series_labels = [s.date.strftime("%b %Y") for s in series.snapshots]
    series_net_worth = [round(s.net_worth, 2) for s in series.snapshots]
    series_assets = [round(s.total_assets, 2) for s in series.snapshots]
    series_liabilities = [round(s.total_liabilities, 2) for s in series.snapshots]

    return templates.TemplateResponse(request, "dashboard.html", {
        "net_worth": nw.net_worth,
        "total_assets": nw.total_assets,
        "total_liabilities": nw.total_liabilities,
        "account_count": account_count,
        "recent_transactions": recent,
        "series_labels": series_labels,
        "series_net_worth": series_net_worth,
        "series_assets": series_assets,
        "series_liabilities": series_liabilities,
    })


def _seed_categories():
    """Insert default categories if the table is empty."""
    import json

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.category import Category

    db = SessionLocal()
    try:
        if db.execute(select(Category)).first() is not None:
            return

        seed_path = Path(__file__).parent / "seeds" / "categories.json"
        if not seed_path.exists():
            return

        with open(seed_path) as f:
            categories = json.load(f)

        for cat in categories:
            db.add(Category(
                name=cat["name"],
                category_type=cat["type"],
                is_system=True,
            ))
        db.commit()
    finally:
        db.close()
