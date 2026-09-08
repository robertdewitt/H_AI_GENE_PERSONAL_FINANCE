from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, init_db
from app.models.account import Account
from app.models.transaction import Transaction
from app.routers import (
    accounts, api, transactions, imports, transfers, net_worth,
    paychecks, valuations, fx, categories, portfolio,
)
from app.routers import (
    app_settings, tasks, scheduled_payments, setup, auth_routes,
    webauthn as webauthn_router, security as security_router,
)
from app.services.net_worth_service import compute_net_worth, compute_net_worth_series
from app.templating import templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _seed_categories()
    _bootstrap_fx_rates()
    _encrypt_legacy_secrets()
    _sync_interest_accruals()
    _sync_scheduled_matches()
    yield


def _sync_scheduled_matches() -> None:
    """Reconcile scheduled payments against transactions already on the ledger.

    Matching used to happen only inside an import, so a payment entered by
    hand, confirmed from an account page, or imported while its schedule was
    worded differently left the schedule sitting overdue while the ledger
    plainly showed it paid. Never fatal to startup.
    """
    import logging

    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.services.scheduled_matcher import backfill_matches

    log = logging.getLogger(__name__)
    db = sessionmaker(bind=engine)()
    try:
        result = backfill_matches(db)
        if result["matched"]:
            db.commit()
            log.info(
                "scheduled payments: %d occurrence(s) matched to existing "
                "transactions over %d pass(es)",
                result["matched"], result["passes"],
            )
        else:
            db.rollback()
    except Exception as exc:
        db.rollback()
        log.warning("scheduled match backfill skipped: %s", exc)
    finally:
        db.close()


def _sync_interest_accruals() -> None:
    """Bring self-calculated interest up to date on every app start.

    Accounts whose balance is their ledger (a personal loan, a financed
    vehicle) have their monthly interest posted by this app rather than by a
    statement. The pass also re-checks earlier months, so a repayment that
    turned up backdated since the last run has its compounding corrected
    rather than left wrong. Never fatal to startup.
    """
    import logging

    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.services.interest_accrual import resync_all_interest_accounts

    log = logging.getLogger(__name__)
    db = sessionmaker(bind=engine)()
    try:
        totals = resync_all_interest_accounts(db)
        if totals["created"] or totals["removed"]:
            db.commit()
            log.info(
                "interest accrual: %d account(s) updated — %d posted, %d rebuilt",
                totals["accounts"], totals["created"], totals["removed"],
            )
        else:
            db.rollback()
    except Exception as exc:
        db.rollback()
        log.warning("interest accrual sync skipped: %s", exc)
    finally:
        db.close()


def _encrypt_legacy_secrets() -> None:
    """One-shot at-rest migration: encrypt any UserProfile API-key columns
    that are still in plaintext from before P2.4. Idempotent — rows that
    are already encrypted are skipped.
    """
    from sqlalchemy.orm import sessionmaker
    from app.database import engine
    from app.services.user_profile_service import encrypt_all_plaintext_api_keys
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        n = encrypt_all_plaintext_api_keys(db)
        if n:
            import logging
            logging.getLogger(__name__).info(
                "encrypted %d UserProfile API key column(s) at rest", n,
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "secret-at-rest migration skipped: %s", exc,
        )
    finally:
        db.close()


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

static_dir = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(imports.router)
app.include_router(transfers.router)
app.include_router(net_worth.router)
app.include_router(paychecks.router)
app.include_router(valuations.router)
app.include_router(fx.router)
app.include_router(categories.router)
app.include_router(app_settings.router)
app.include_router(portfolio.router)
app.include_router(tasks.router)
app.include_router(scheduled_payments.router)
app.include_router(api.router)
app.include_router(setup.router)
app.include_router(auth_routes.router)
app.include_router(webauthn_router.router)
app.include_router(security_router.router)


# ── Auth gate middleware ─────────────────────────────────────────────
# Phase 2.2: every HTML route requires either a /setup-first-run, a
# valid session cookie, or a Bearer token. /login, /setup, /static, and
# /api/* are exempt from the redirect (API routes handle their own 401
# via the get_current_user dependency).
_PUBLIC_PATHS = (
    "/setup", "/login", "/logout", "/static", "/api/", "/favicon.ico",
    # WebAuthn login ceremony must be reachable for unauthenticated callers
    # (the user is *trying* to sign in). Registration ceremony is gated by
    # get_current_user inside the router and stays protected.
    "/auth/webauthn/login/options", "/auth/webauthn/login/verify",
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p) for p in _PUBLIC_PATHS):
        return await call_next(request)

    from sqlalchemy.orm import sessionmaker
    from app.database import engine
    from app.services.sessions import lookup_session
    from fastapi.responses import RedirectResponse

    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        if setup.needs_setup(db):
            return RedirectResponse(url="/setup", status_code=303)
        raw = request.cookies.get("session")
        sess = lookup_session(db, raw) if raw else None
    except Exception as exc:
        # Fail CLOSED. This used to be `pass`, which fell through to the
        # protected route — so any database error (SQLite's "database is
        # locked" happens under ordinary use) let a request with an arbitrary
        # session cookie straight in. An unverifiable session is no session.
        import logging
        # Deliberately not .exception(): a SQLAlchemy traceback embeds the
        # failing statement and its bound parameters, which would put
        # ledger data back into the log that turning off SQL echo just
        # took out of it.
        logging.getLogger(__name__).warning(
            "auth gate could not verify the session; denying %s (%s)",
            path, exc.__class__.__name__,
        )
        sess = None
    finally:
        db.close()

    if sess is None:
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(
            url=f"/login?return_to={target}", status_code=303,
        )

    return await call_next(request)


# Registered after the auth gate so it ends up *outermost*: add_middleware
# prepends, and the body has to be refused before anything else runs —
# before a DB session is opened for the gate, and long before FastAPI
# parses the multipart body. The upload route's own cap cannot do this: it
# only sees the upload once the whole body is already spooled to disk.
from app.middleware.body_limit import BodySizeLimitMiddleware

app.add_middleware(
    BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes,
)


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


def _bootstrap_fx_rates():
    """On startup, ensure we have ~5 years of daily FX history for key pairs.

    Runs in a background thread so it doesn't block the server from starting.
    """
    import logging
    import threading

    log = logging.getLogger(__name__)

    def _sync():
        from datetime import datetime, timedelta
        from app.services.clock import naive_utc_now

        from sqlalchemy import func as sa_func

        from app.database import SessionLocal
        from app.models.currency_rate import CurrencyRate
        from app.services.fx_rate_fetcher import sync_current_rates, sync_historical_rates
        from app.services.user_profile_service import get_profile

        db = SessionLocal()
        try:
            base = settings.base_currency
            # Always sync today's rates for key currencies
            profile = get_profile(db)
            current_quotes = list({
                "GBP", "EUR", "JPY", "AUD", "CAD", "CHF",
                "HKD", "SGD", "NZD",
                profile.display_currency,
            } - {base})
            try:
                sync_current_rates(db, base=base, quotes=current_quotes)
            except Exception as exc:
                log.warning("FX startup current-rate sync failed: %s", exc)

            key_quotes = ["GBP", "EUR", "JPY"]
            five_years_ago = naive_utc_now() - timedelta(days=5 * 365)

            for quote in key_quotes:
                if quote == base:
                    continue

                count = db.execute(
                    sa_func.count(CurrencyRate.id).select().where(
                        CurrencyRate.base_currency == base,
                        CurrencyRate.quote_currency == quote,
                    )
                ).scalar() or 0

                if count >= 1200:
                    log.info(
                        "FX bootstrap: %s/%s already has %d rates, skipping",
                        base, quote, count,
                    )
                    continue

                log.info(
                    "FX bootstrap: fetching 5-year history for %s/%s "
                    "(currently %d rates)...",
                    base, quote, count,
                )
                try:
                    stored = sync_historical_rates(
                        db, base=base, quote=quote,
                        start_date=five_years_ago,
                    )
                    log.info(
                        "FX bootstrap: stored %d rates for %s/%s",
                        stored, base, quote,
                    )
                except Exception as exc:
                    log.warning(
                        "FX bootstrap: failed for %s/%s: %s",
                        base, quote, exc,
                    )
        finally:
            db.close()

    threading.Thread(target=_sync, name="fx-bootstrap", daemon=True).start()
