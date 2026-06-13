# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the app (hot-reload, SQLite by default)
source venv/bin/activate
python run.py
# → http://127.0.0.1:8000  |  API docs: http://127.0.0.1:8000/docs

# Run all tests
pytest

# Run a single test file
pytest tests/test_truth_engine.py

# Run a single test by name
pytest tests/test_truth_engine.py::test_split_invariant

# Use PostgreSQL instead of SQLite
DB_BACKEND=postgresql DATABASE_URL=postgresql://user:password@localhost:5432/financial_hygiene python run.py
```

No separate build step — `init_db()` runs on startup and is idempotent (creates tables + indexes, adds missing columns via `_sqlite_add_column_if_missing`).

## Architecture

### Two-tier API

The same FastAPI backend serves two audiences:

- **Human UI** — server-rendered Jinja2 templates (`app/routers/` excluding `api.py`)
- **LLM Agent JSON API** — `app/routers/api.py`, prefix `/api/v1/`, returns structured payloads designed for agent consumption

The primary agent entry point is `GET /api/v1/agent/context` — a single-call comprehensive snapshot. The full agent context spec is in `docs/TRUTH_MODEL.md`.

### Data Flow

```
Raw Import → Canonical Transaction → Truth Engine → Splits/Reconciliation → Snapshots → Agent Context
```

Raw imported rows are **not** truth. The truth layer adds:

1. **`EconomicEventType`** on each transaction (`app/models/enums.py`) — narrow economic role (e.g. `card_payment_settlement`, `mortgage_principal`), not a reporting category
2. **`TransactionSplit`** (`app/models/transaction_split.py`) — one or more canonical allocations per transaction; spend analysis reads splits, **never raw amounts**
3. **`ReconciliationGroup`** (`app/models/reconciliation.py`) — N-member groups replacing pairwise transfer links; invariant: members' `allocated_amount_base` nets to zero within tolerance
4. **`PaymentDecomposition`** (`app/models/payment_decomposition.py`) — breaks liability payments into principal/interest/escrow/fee components

### Key Invariants

- Sum of `TransactionSplit.amount_native` == parent transaction amount (within tolerance)
- `ReconciliationMember.allocated_amount_base` nets to zero within `ReconciliationGroup.tolerance_base`
- Sum of `PaymentDecomposition` components == transaction amount
- Every balance has: `provenance`, `confidence`, `as_of_date`

### Service Layer (`app/services/`)

| Service | Purpose |
|---|---|
| `event_classifier.py` | Assigns `EconomicEventType` with provenance + confidence |
| `split_service.py` / `split_auto.py` | Creates/manages `TransactionSplit` rows |
| `auto_reconciliation.py` | Suggests `ReconciliationGroup` rows for transfer pairs |
| `reconciliation_invariants.py` | Validates net-zero invariant on groups |
| `spend_analysis.py` | True spend from splits (not raw transactions) |
| `attribution.py` | Explains ΔNW = contributions + market + FX + principal + fees + spending |
| `data_quality.py` | `DataQualityReport` with blockers, warnings, counters, close-readiness score |
| `snapshot_service.py` | `AccountBalanceSnapshot`, `HouseholdSnapshot` time series |
| `transaction_truth.py` | Balance truth dispatch — selects correct balance source per account |
| `document_parse.py` | Parses payslip/rental JSON into `ParsedFinancialDocument` |
| `document_apply.py` | Persists document, lines, transaction, splits, and rental P&L snapshot |
| `import_service.py` | Batch CSV/XLS import with date format detection and liability sign-flip |
| `categorizer.py` | Three-tier auto-categorization: learned rules → keyword heuristics → Ollama LLM |
| `transfer_detector.py` | Transfer matching with confidence scoring |
| `net_worth_service.py` | FX-aware net worth with time series |
| `fx_service.py` / `fx_rate_fetcher.py` | Exchange rate management; Yahoo Finance + ECB sources |

### Balance Truth Sources

`account.balance_truth_source` controls how balance is computed for each account:

| Source | Mechanism |
|---|---|
| `transaction_sum` | SUM(transactions.amount) — default |
| `latest_statement` | `account.statement_balance` snapshot |
| `latest_valuation` | Most recent `AssetValuation` row |
| `liability_balance` | Statement or principal balance |
| `manual_mark` | `account.current_value` |
| `hybrid` | Transaction sum, falling back to statement |

### Schema Migrations

Both Alembic and `init_db()` operate side by side:

- **Alembic** is the source of truth going forward — `alembic upgrade head`
  applies pending revisions, `alembic revision --autogenerate -m "…"` after
  editing models generates the next one. Baseline lives in
  `alembic/versions/`.
- **`init_db()`** in `app/database.py` still runs on every startup so a
  fresh SQLite database is usable without running Alembic. New columns on
  existing tables go through `_col(table, column, type, default)` (the
  helper around `_add_column_if_missing`); new tables flow through
  `Base.metadata.create_all`. All `init_db` changes must be additive and
  idempotent — Alembic owns destructive / structural changes.

### Multi-user auth (`app/services/`)

- `auth.py` — `get_current_user` FastAPI dependency. Resolves either the
  `session` cookie or `Authorization: Bearer <token>` (API tokens); HTML
  routes 303 to `/login`, API routes 401. `require_admin` is the
  admin-only variant for `/register`.
- `sessions.py` — server-side session create / lookup / revoke. Cookie
  carries a 256-bit random token; only the SHA-256 hash is persisted.
  Idle expiry 7 days, absolute 30.
- `webauthn_service.py` + `app/routers/webauthn.py` — passkey ceremonies
  (registration + authentication). Pending challenges stored in a
  5-minute TTL in-memory dict (swap to Redis for multi-worker).
- `scoping.py` — `owned_accounts`, `owned_account_ids`,
  `get_owned_account_or_404`, `owned_transaction_query` and friends.
  Every router / service entry point that touches owned tables goes
  through these instead of `select(Account)` directly. The route-walking
  test in `tests/test_tenant_isolation.py` fails closed if a new route
  forgets.
- `setup_claim.py` — first-run flow: backup → user create → claim all
  existing rows under the new user_id → integrity check → commit. Used
  by `app/routers/setup.py`.
- `rate_limit.py` — sliding-window limiter; `/login` is 10 attempts /
  15 minutes per `(ip, username)`.
- `secret_box.py` — Fernet encrypt/decrypt at rest. Fernet key is
  HKDF-derived from `SECRET_KEY` (generated into `./.env` on first
  launch if missing). `get_profile()` decrypts the encrypted columns
  in-place via `attributes.set_committed_value` so callers see plaintext
  but the DB stores `fernet:…`. The `mask_secret` Jinja filter renders
  the trailing four chars only for display.
- `upload_safety.py` — `safe_upload_dest(upload_dir, name, user_id=...)`
  places uploads under `uploads/<user_id>/` and `assert_user_owns_path`
  is the guard every confirm endpoint runs before opening a
  form-supplied filepath.

### Owned-tables convention

Top-level owned tables (`accounts`, `categories`, `category_rules`,
`import_batches`, `rental_properties`, `asset_valuations`, scheduled
payments, plan_it_plans, all `*_snapshots`, `financial_documents`,
`property_pnl_snapshots`, `user_profile`) carry a nullable `user_id` FK.
Reachable-via-account tables (`transactions`, `transaction_splits`,
`transfer_links`, `reconciliation_*`, `payment_decompositions`) do NOT
denormalize `user_id` — they join through `Account` for isolation.

### Configuration (`app/config.py`)

Key settings via env or `.env` file:

| Variable | Default | Purpose |
|---|---|---|
| `DB_BACKEND` | `sqlite` | `sqlite` or `postgresql` |
| `DATABASE_URL` | `sqlite:///data/finance.db` | Full DB URL |
| `BASE_CURRENCY` | `USD` | Base currency for FX conversions |
| `IMPORT_BATCH_SIZE` | `5000` | Rows flushed per batch during imports |
| `DATE_DAYFIRST` | `True` | DD/MM (True) vs MM/DD (False) default |
| `RP_ID` | `localhost` | WebAuthn relying-party id (hostname, no scheme) |
| `RP_ORIGIN` | `http://localhost:8000` | WebAuthn origin sent in ceremonies |
| `RP_NAME` | `Financial Hygiene` | Label shown by the OS biometric prompt |
| `SECRET_KEY` | _auto-generated_ | HKDF seed for at-rest encryption — back up `.env` |

### Structured Financial Documents

Payslips and rental statements are first-class documents — not inferred from bank rows. The flow:

1. JSON validated by `document_parse.parse_document_dict` → `ParsedFinancialDocument`
2. Persisted by `document_apply.apply_financial_document` → creates `FinancialDocument` + `FinancialDocumentLine` rows, links to `Transaction`, creates `TransactionSplit` per line, creates `PropertyPnLSnapshot` for rental statements
3. Sample fixtures in `tests/fixtures/documents/`

### Tests

Tests use an in-memory SQLite database — no fixtures file needed. The `db` pytest fixture in each test file creates a fresh `Base.metadata.create_all` session. See `tests/test_truth_engine.py` for the pattern.

Important regression suites worth keeping green:

| File | What it guards |
|---|---|
| `test_net_worth_series_queries.py` | `compute_net_worth_series(months=24)` must issue < 10 SQL statements |
| `test_upload_safety.py` | Path traversal blocked; per-user dirs; ownership guard rejects peers |
| `test_setup_claim.py` | First-run claim backs up, attributes every row, integrity check passes, net worth unchanged |
| `test_tenant_isolation.py` | Anonymous can't reach `/api/v1/*`; cross-user access yields empty/404; route-walks every registered endpoint |
| `test_secret_box.py` | Fernet round-trip; mask reveals last 4 chars only; wrong key returns None instead of raising |
| `test_recurring_forecast_knobs.py` | UserProfile knobs (stale_days, moving_avg_months, etc.) influence detection on the next call — no caching |
