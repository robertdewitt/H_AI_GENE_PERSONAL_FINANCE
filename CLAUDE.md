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

There is no Alembic migration workflow in active use. Schema changes are applied via `init_db()` in `app/database.py`:

- New tables: use `Base.metadata.create_all` (all models must be imported in `app/models/__init__.py`)
- New columns on existing tables: add a `_sqlite_add_column_if_missing(conn, table, column, type, default)` call in `init_db()`
- All changes must be additive and idempotent

### Configuration (`app/config.py`)

Key settings via env or `.env` file:

| Variable | Default | Purpose |
|---|---|---|
| `DB_BACKEND` | `sqlite` | `sqlite` or `postgresql` |
| `DATABASE_URL` | `sqlite:///data/finance.db` | Full DB URL |
| `BASE_CURRENCY` | `USD` | Base currency for FX conversions |
| `IMPORT_BATCH_SIZE` | `5000` | Rows flushed per batch during imports |
| `DATE_DAYFIRST` | `True` | DD/MM (True) vs MM/DD (False) default |

### Structured Financial Documents

Payslips and rental statements are first-class documents — not inferred from bank rows. The flow:

1. JSON validated by `document_parse.parse_document_dict` → `ParsedFinancialDocument`
2. Persisted by `document_apply.apply_financial_document` → creates `FinancialDocument` + `FinancialDocumentLine` rows, links to `Transaction`, creates `TransactionSplit` per line, creates `PropertyPnLSnapshot` for rental statements
3. Sample fixtures in `tests/fixtures/documents/`

### Tests

Tests use an in-memory SQLite database — no fixtures file needed. The `db` pytest fixture in each test file creates a fresh `Base.metadata.create_all` session. See `tests/test_truth_engine.py` for the pattern.
