# Financial Hygiene — Personal Finance Truth Engine

A personal finance **data and truth layer**: multi-currency accounts, auditable balances, transaction **splits** (semantic allocations), **reconciliation groups**, structured **documents** (payslips, rental statements), and agent-oriented JSON APIs. It is **not** a budgeting app first—it is infrastructure for accurate net worth, spend semantics, and LLM agents that must see **confidence, freshness, and gaps**.

The app also serves **humans** via server-rendered HTML (Pico CSS, Chart.js) for import, review, and dashboards.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (Human UI)                       │
│    Jinja2 · Pico CSS · Chart.js                                 │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Application                        │
│  HTML: /accounts /transactions /imports /transfers /net-worth … │
│  JSON: /api/v1/*  ·  OpenAPI /docs                              │
│                                                                 │
│  Truth layer: economic event types · splits · reconciliation    │
│  · payment decomposition · balance truth sources · snapshots    │
│  · structured documents · data quality · attribution          │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  Services: account_service · split_service · import_service     │
│  · event_classifier · reconciliation_invariants · data_quality  │
│  · document_parse / document_apply · snapshot_service            │
│  · attribution · auto_reconciliation · split_auto              │
│  · net_worth_service · fx_service · categorizer · …             │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  SQLAlchemy ORM — SQLite (default) or PostgreSQL               │
│  See docs/TRUTH_MODEL.md for schema philosophy                  │
└─────────────────────────────────────────────────────────────────┘
```

**Audiences**

1. **Humans** — Import, categorize, **edit transactions including splits**, transfers, valuations, paychecks.
2. **LLM agents** — `GET /api/v1/…` for structured, qualified data (balances, spend-from-splits, data quality blockers, attribution).

Full truth-design reference: **[docs/TRUTH_MODEL.md](docs/TRUTH_MODEL.md)**.

## LLM / Agent API

### Bootstrap

1. **`GET /api/v1/agent/context`** — Net worth, accounts, recent flows, hints to other endpoints.
2. **`GET /api/v1/data-quality`** — **Blockers** and **warnings** first; `close_readiness_score` is secondary; structured **counters** (uncategorized, unsplit, reconciliation FX gaps, …).
3. **`GET /api/v1/balance-sheet`** — Full balance sheet with **confidence** and **staleness** per account.

### Core JSON endpoints

| Endpoint | Description |
|---|---|
| `GET /api/v1/agent/context` | Single-call overview for agents |
| `GET /api/v1/accounts` | Accounts with balances (native + base) |
| `GET /api/v1/transactions` | Filtered/paginated transactions (`event_type`, …) |
| `GET /api/v1/categories` | Categories with stats |
| `GET /api/v1/spending/by-category` | Category totals (raw rows; use true-spend for semantics) |
| `GET /api/v1/spending/monthly` | Monthly income vs spending (non-transfer filtered) |
| `GET /api/v1/spending/true-spend` | **Spend from splits only** — by `spend_type` / category |
| `GET /api/v1/spending/top-merchants` | Top merchants |
| `GET /api/v1/net-worth` | Current net worth + breakdown |
| `GET /api/v1/net-worth/history` | Monthly net worth series |
| `GET /api/v1/balance-sheet` | Balance sheet + confidence + FX metadata |
| `GET /api/v1/data-quality` | Blockers, warnings, counters, score |
| `GET /api/v1/documents/payroll` | Payroll document time series |
| `GET /api/v1/rental-properties` | Rental property entities |
| `GET /api/v1/rental-properties/{id}/pnl` | Property P&L snapshots |
| `GET /api/v1/instruments` | Securities / instruments (foundation) |
| `POST /api/v1/reconciliation/auto-suggest` | Create **suggested** transfer reconciliation groups |
| `GET /api/v1/attribution/net-worth-change?start=&end=` | NW change decomposition (flows + valuation diff + FX translation) |

OpenAPI: **`/docs`**.

### Auto-categorization (optional)

Rules → keywords → **Ollama** (local). See project setup for `ollama pull`.

### Import date detection

DD/MM vs MM/DD detection with confidence on the import mapping UI.

## Features

- **Truth layer** — `event_type` (economic role), classification provenance/confidence, balance truth sources, staleness hints.
- **Transaction splits** — Multiple allocations per transaction; sum must match transaction amount. Editable on **Transaction edit** page; pass-through split created on **import** when missing.
- **Reconciliation groups** — N-member transfer/settlement groups with explicit allocations and FX-aware validation.
- **Structured documents** — Payroll and rental JSON → `FinancialDocument` + lines + parent transaction + splits; property P&L snapshots.
- **Payment decomposition** — Liability payments into principal/interest/escrow/… with validation.
- **Data quality** — Blockers/warnings + counters (e.g. multi-currency recon without FX → **blocker**).
- **Attribution** — Net worth change breakdown (income, flows, fees, valuation **market** movement, **FX** translation approximation).
- **Household / account snapshots** — Stored time series for balances and rollups.
- **Accounts** — Banking, cards, investments, pensions, real estate, vehicles, loans, mortgages, etc.; multi-currency; FX bootstrap (Yahoo/Frankfurter).
- **CSV/XLS import** — Column detection, large batching, liability sign handling, **event classification** + **default splits** after import.
- **Transfers** — Detection, linking, **auto-suggested reconciliation groups** via API.
- **Net worth** — FX-aware totals and history.
- **Paychecks** — Stub import/manual entry.
- **Asset valuations** — History for illiquid assets.
- **Currency converter** — Stored rates.

## Roadmap (optional)

- Deeper brokerage **lot** / **price** sync (models exist; wiring TBD).
- Budgeting and proactive alerts (out of scope for core truth layer).

## Quick Start

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

- App: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- API docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

On the first visit every page redirects to **/setup**, where you register
an admin account (username + display name + password ≥ 10 chars). On
submit the app:

1. Writes a timestamped backup to `data/backups/pre_auth_<UTC>.db`,
2. Creates your user and attributes every existing row in one transaction,
3. Runs an integrity check (row counts unchanged, no NULL `user_id`),
4. Logs you in via a session cookie.

If the backup or integrity check fails, the transaction rolls back and
the database is left in its pre-claim state. Subsequent users (created
via `/register`, admin-only) follow the same flow without the claim step.

### Sign-in

The default sign-in is **WebAuthn (passkeys)** — Touch ID, Windows Hello,
or your phone's biometric — with a password fallback. Add a passkey from
**Settings → Security & passkeys** once you're signed in.

WebAuthn requires a **secure context**. `http://localhost` /
`http://127.0.0.1` qualify; any other origin needs HTTPS. For LAN or
remote deployments set the relying-party identity via env:

| Variable | Example | Purpose |
|---|---|---|
| `RP_ID` | `finance.example.com` | Hostname the browser sees (no scheme/port) |
| `RP_ORIGIN` | `https://finance.example.com` | Full origin sent in WebAuthn ceremonies |
| `RP_NAME` | `Finance — Home` | Label shown by the OS biometric prompt |

### API tokens for agents

LLM agents (and any non-browser caller) talk to `/api/v1/*` with a Bearer
token. Mint one from **Settings → Security & passkeys → API tokens** —
the value is shown exactly once; only the SHA-256 hash is stored.

```bash
TOKEN="paste-the-once-shown-token-here"
curl -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:8000/api/v1/agent/context
```

Revoking a token from the same page invalidates it immediately.

### Secrets at rest

Third-party API keys (Rentcast, PropertyData, Domain) are encrypted in
the database with Fernet, keyed by `SECRET_KEY`. The first launch
auto-generates a `SECRET_KEY` into `./.env` if one isn't present and
warns you to back it up — losing that file renders every encrypted
column unreadable.

### Optional: Ollama for categorization

```bash
ollama pull llama3.2
```

### PostgreSQL

```bash
DB_BACKEND=postgresql
DATABASE_URL=postgresql://user:password@localhost:5432/financial_hygiene
```

### Schema migrations (Alembic)

`init_db()` still applies idempotent additive migrations on every
startup so a fresh SQLite database is usable without ceremony. Alembic
is the source of truth going forward:

```bash
# Apply pending migrations
alembic upgrade head

# Generate a new revision after editing models
alembic revision --autogenerate -m "what changed"
```

## Workflow

1. **Add accounts** — Banks, cards, property, vehicles, loans, etc.
2. **Import transactions** — CSV/XLS; column + date format detection; classification + default splits.
3. **Edit transactions** — Date, amount, category, **economic event type**, **splits** (amounts must sum to transaction total), transfers.
4. **Categories & rules** — Teach patterns; optional Ollama fallback.
5. **FX** — Rates bootstrap on startup; manual/converter as needed.
6. **Transfers** — Review; **POST `/api/v1/reconciliation/auto-suggest`** for suggested groups.
7. **Valuations & paychecks** — As needed.
8. **Structured documents** — Payroll/rental JSON pipelines (see `tests/fixtures/documents/`, `document_apply` service).
9. **Agents** — Use `/api/v1/agent/context`, `/api/v1/data-quality`, `/api/v1/balance-sheet`, `/api/v1/spending/true-spend`.

## Project structure (high level)

```
app/
├── main.py                 # App, lifespan (init_db, FX bootstrap, categories)
├── config.py
├── database.py             # Engine + SQLite migrations (additive columns/indexes)
├── models/                 # Account, Transaction, TransactionSplit, Category,
│                           # Reconciliation*, PaymentDecomposition,
│                           # FinancialDocument*, RentalProperty, snapshots,
│                           # Instrument/PositionLot/PriceSnapshot, …
├── routers/                # accounts, transactions, imports, transfers, api, …
├── services/               # Truth + domain services (see TRUTH_MODEL.md)
├── templates/
├── static/
├── seeds/
docs/
├── TRUTH_MODEL.md          # Architecture & migration notes
tests/
├── test_truth_engine.py
├── test_structured_documents.py
└── fixtures/documents/     # Sample payroll / rental JSON
```

## Tech stack

- Python **3.11+**, **FastAPI**, **SQLAlchemy**, **SQLite** / **PostgreSQL**
- **Pico CSS**, **Chart.js**
- **Pandas** for imports
- Optional **Ollama**; **yfinance** / **Frankfurter** for FX

## Performance

The hot paths that an interactive UI hits — net-worth dashboard, time
series, balance sheet — go through batched balance helpers
(`get_many_account_balances_rich` / `_series` in
`app/services/account_service.py`). A 24-month net-worth series across
~15 accounts issues **< 10 SQL statements** total; doubling the window
does not (anywhere close to) double the SQL count. There's a regression
test (`tests/test_net_worth_series_queries.py`) that fails-closed if
that property ever regresses.

Other things that survive scale: composite indexes on
`(account_id, date)` etc., SQLite WAL, batched imports keyed by
`IMPORT_BATCH_SIZE`, optional PostgreSQL pooling.

## Multi-user model

* Each user owns their data via a `user_id` column on every top-level
  table (accounts, categories, import batches, snapshots, scheduled
  payments, plans, user profile, …).
* Transactions and other "reachable via account" rows inherit ownership
  from their account — queries always join through `Account` to enforce
  isolation.
* `app/services/scoping.py` provides the canonical helpers
  (`owned_accounts`, `owned_transaction_query`,
  `get_owned_account_or_404`). Routers and services use these instead
  of hand-rolling `WHERE user_id = …`.
* A route-walking isolation test (`tests/test_tenant_isolation.py`)
  iterates over every registered route and asserts anonymous callers
  cannot reach `/api/v1/*` and HTML routes cannot bypass the auth
  redirect — new endpoints fail closed automatically.
* Uploads live under `uploads/<user_id>/`. Every confirm endpoint
  verifies the supplied filepath sits inside the current user's
  directory before reading it.

## Tests

```bash
pytest tests/
```
