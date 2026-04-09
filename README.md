# Financial Hygiene — Personal Finance Dashboard

A comprehensive personal finance application that tracks transactions across multiple currencies, detects transfers between accounts, calculates net worth across all asset classes, and provides financial insights with auto-categorization. The entire platform is designed as a **data capture and analysis layer for LLM agents** that can reason about spending habits, investments, and financial health.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (Human UI)                       │
│    Jinja2 Templates · Pico CSS · Chart.js · HTMX-ready         │
└──────────────────────────────┬──────────────────────────────────┘
                               │  HTML pages
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Application                        │
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────────────┐  │
│  │ HTML Routers│  │  JSON API   │  │  OpenAPI / Swagger UI  │  │
│  │ /accounts   │  │ /api/v1/... │  │  /docs  /openapi.json  │  │
│  │ /transactions│ │             │  └────────────────────────┘  │
│  │ /net-worth  │  │ Structured  │                              │
│  │ /imports    │  │ data for    │                              │
│  │ /transfers  │  │ LLM agents  │                              │
│  │ /categories │  │             │                              │
│  │ /fx         │  │             │                              │
│  └──────┬──────┘  └──────┬──────┘                              │
│         │                │                                      │
│         ▼                ▼                                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                   Service Layer                          │   │
│  │  account_service · categorizer · transfer_detector       │   │
│  │  net_worth_service · fx_service · import_service         │   │
│  │  paycheck_service · asset_valuation_service              │   │
│  └──────────────────────────┬──────────────────────────────┘   │
│                              │                                  │
│  ┌──────────────────────────┴──────────────────────────────┐   │
│  │                   Data Layer (SQLAlchemy)                 │   │
│  │  9 ORM Models · Composite indexes · WAL mode             │   │
│  └──────────────────────────┬──────────────────────────────┘   │
└──────────────────────────────┼──────────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
        ┌──────────┐   ┌────────────┐   ┌─────────────┐
        │  SQLite   │   │ PostgreSQL │   │  Ollama LLM │
        │  (default)│   │ (optional) │   │  (optional)  │
        └──────────┘   └────────────┘   └─────────────┘
```

The application serves two audiences through the same backend:

1. **Humans** — Server-rendered HTML pages for importing data, reviewing transactions, editing categories, and viewing dashboards.
2. **LLM Agents** — A structured JSON API (`/api/v1/`) providing the same financial data in a format optimized for programmatic analysis.

## LLM Integration

### How agents use the data

The JSON API is designed so an LLM agent can understand your full financial picture and provide actionable advice. An agent workflow looks like:

1. **Bootstrap context** — Call `GET /api/v1/agent/context` to get a comprehensive snapshot: net worth, all account balances, 30-day income/spending by category, recurring expenses with estimated monthly cost, largest recent transactions, and data quality metrics.
2. **Deep-dive** — Use specific endpoints to explore areas of interest:
   - `/api/v1/spending/by-category?months=6` — Where is the money going?
   - `/api/v1/spending/monthly?months=12` — Income vs spending trend
   - `/api/v1/spending/top-merchants?months=3` — Recurring subscriptions and high-spend merchants
   - `/api/v1/transactions?search=amazon&limit=50` — Drill into specific merchants
   - `/api/v1/net-worth/history?months=24` — Is net worth growing or shrinking?
3. **Recommend** — With structured data, the agent can identify: overspending categories, unnecessary subscriptions, savings rate trends, debt payoff strategies, and investment allocation gaps.

### JSON API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/v1/agent/context` | **Single-call context** — everything an agent needs to start reasoning |
| `GET /api/v1/accounts` | All accounts with native + base currency balances |
| `GET /api/v1/transactions` | Filtered/paginated transactions (by account, category, date, search, etc.) |
| `GET /api/v1/categories` | Categories with transaction counts and totals |
| `GET /api/v1/spending/by-category` | Spending breakdown by category for N months |
| `GET /api/v1/spending/monthly` | Monthly income vs spending for trend analysis |
| `GET /api/v1/spending/top-merchants` | Top merchants by spend, flags likely recurring expenses |
| `GET /api/v1/net-worth` | Current net worth with full account breakdown |
| `GET /api/v1/net-worth/history` | Monthly net worth time series |

All endpoints are self-documented via **OpenAPI/Swagger** at `/docs`.

### Auto-categorization with LLMs

Transaction categorization uses a three-tier engine:

1. **Learned rules** — When a user corrects a category, the system saves a pattern and retroactively updates all matching transactions (past and future).
2. **Keyword heuristics** — Built-in mappings for common merchants (groceries, dining, gas, subscriptions, etc.).
3. **Ollama LLM fallback** — For unrecognized descriptions, a local LLM (llama3.2 via [Ollama](https://ollama.com)) classifies the transaction. The LLM runs locally and is free — no API keys needed.

### Agentic date format detection

When importing transaction files, the system scans the date column and auto-detects whether dates are DD/MM (UK/EU) or MM/DD (US) format. It uses multiple signals — values > 12 that can only be a day, chronological ordering analysis, value distribution — and reports its confidence and reasoning on the import mapping page. The user can override the detection with a single click.

## Features

- **Account Management** — Track bank accounts, credit cards, investments, IRAs, pensions, real estate, vehicles, collectibles, and more
- **Multi-Currency / FX Support** — Accounts in any currency with proper currency symbols (£, €, ¥, etc.); live rate fetching from Yahoo Finance and ECB; automatic conversion to base currency for net worth
- **FX Rate Bootstrap** — On startup, automatically fetches 5 years of daily historical rates for USD/GBP, USD/EUR, and USD/JPY
- **CSV/XLS Import** — Upload transaction files with automatic column detection, agentic date format detection, manual mapping override, and batch processing optimized for 1–10M+ transactions
- **Liability Sign-Flip** — Credit card charges, loan payments, and mortgage transactions are automatically sign-corrected on import so balances reflect money owed
- **Transaction CRUD** — Edit or delete any individual transaction; bulk select multiple to set category, mark as transfer, or delete. Filter state is preserved across edits.
- **Auto-Categorization** — Three-tier engine: learned rules → keyword heuristics → local LLM (Ollama)
- **Category Management** — Add, edit, and delete categories; view transaction counts per category; manage learned rules with hit counts
- **Spending Summaries** — Category breakdown tables and charts on both account detail pages and the net worth page
- **Transfer Detection** — Detects transfers including payments, credits, ACH, and autopay; confidence scoring with bulk-confirm; payment scanning for liability accounts
- **Net Worth Tracking** — FX-aware net worth calculation with time-series charts and asset group breakdowns
- **Paycheck Stub Tracking** — Import or manually enter paycheck data with full tax, deduction, and benefit breakdowns
- **Asset Valuations** — Manual valuation entry for real estate, vehicles, collectibles, pensions with history tracking
- **Currency Converter** — Convert amounts using stored FX rates

## Upcoming Features

- **Phase 2**: Budgeting, spending recommendations, recurring transaction detection
- **Phase 3**: Retirement planning with Monte Carlo simulations
- **Phase 4**: Automated bank imports (Plaid API), alerts, scheduled refresh
- **Phase 5**: Agent-driven insights — connect the API to an LLM agent that proactively identifies savings opportunities, debt payoff strategies, and investment rebalancing

## Quick Start

```bash
# Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the app (SQLite — zero config)
python run.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.
API docs at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

### Optional: Local LLM for auto-categorization

Install [Ollama](https://ollama.com), then pull the model:

```bash
ollama pull llama3.2
```

The categorizer will use it automatically when rules and keywords don't match. If Ollama isn't running, it silently skips the LLM step — no errors.

### Using PostgreSQL (for large-scale datasets)

Set these environment variables (or create a `.env` file):

```bash
DB_BACKEND=postgresql
DATABASE_URL=postgresql://user:password@localhost:5432/financial_hygiene
```

## Workflow

1. **Add accounts** — Create accounts for each bank, brokerage, credit card, property, vehicle, etc.
2. **Upload transaction files** — Import CSV/XLS exports; columns and date format are auto-detected with manual override. Transactions are auto-categorized on import.
3. **Review & edit transactions** — Edit individual transactions or bulk-select to set categories, mark transfers, or delete. Category corrections teach the system for future imports.
4. **Manage categories** — Add/edit/delete categories at `/categories`; view learned rules and their hit counts.
5. **FX rates load automatically** — On startup, 5 years of daily rates for GBP, EUR, and JPY are fetched. Additional pairs can be fetched or entered manually.
6. **Review transfers** — Scan for payment-like transactions; confirm transfer pairs individually or in bulk.
7. **Value assets** — For real estate, vehicles, and collectibles, add periodic valuations.
8. **Upload paychecks** — Track income details including taxes, 401(k), and benefits.
9. **View net worth** — See your complete financial picture across all currencies and asset types.
10. **Query via API** — Use `/api/v1/agent/context` or specific endpoints to feed data to LLM agents for analysis and recommendations.

## Project Structure

```
app/
├── main.py              # FastAPI app, startup bootstrap (FX, categories)
├── config.py            # Settings (DB, base currency, date format, batch size)
├── database.py          # SQLAlchemy setup (SQLite / PostgreSQL)
├── models/              # ORM models (9 tables)
│   ├── account.py       # Accounts with asset/liability classification
│   ├── transaction.py   # Transactions with FX fields
│   ├── category.py      # Hierarchical categories
│   ├── category_rule.py # Learned categorization rules
│   ├── transfer_link.py # Transfer pairs
│   ├── import_batch.py  # Import tracking
│   ├── currency_rate.py # Historical FX rates
│   ├── paycheck_stub.py # Paycheck data
│   └── asset_valuation.py # Asset valuations
├── schemas/             # Pydantic validation schemas
├── routers/             # Route handlers (10 modules)
│   ├── accounts.py      # Account CRUD + detail with spending summary
│   ├── transactions.py  # Transaction CRUD, bulk edit, auto-categorize
│   ├── categories.py    # Category management + learned rules
│   ├── imports.py       # CSV/XLS upload with date detection + auto-categorize
│   ├── transfers.py     # Transfer detection, payment scanning, management
│   ├── net_worth.py     # Net worth + category spending summary
│   ├── valuations.py    # Asset valuation management
│   ├── paychecks.py     # Paycheck stub management
│   ├── fx.py            # FX rates, live fetching, converter
│   └── api.py           # JSON API for LLM agents (/api/v1/)
├── services/            # Business logic
│   ├── account_service.py          # CRUD + FX-aware balance
│   ├── import_service.py           # Batch import, date detection, liability sign-flip
│   ├── categorizer.py              # Auto-categorization (rules + keywords + Ollama)
│   ├── transfer_detector.py        # Transfer matching + payment scanning
│   ├── net_worth_service.py        # FX-aware net worth
│   ├── fx_service.py               # Exchange rate management
│   ├── fx_rate_fetcher.py          # Yahoo Finance + ECB rate fetching
│   ├── paycheck_service.py         # Paycheck stub parsing
│   └── asset_valuation_service.py  # Asset valuation management
├── templates/           # Jinja2 HTML templates
├── static/              # CSS, JS
└── seeds/               # Default category data (33 categories)
```

## Tech Stack

- **Python 3.11+** with **FastAPI**
- **SQLite** (default) or **PostgreSQL** via **SQLAlchemy** ORM
- **Pico CSS** + **Chart.js** for the UI
- **Pandas** for CSV/XLS parsing (batch-optimized for millions of rows)
- **Ollama** (optional) for LLM-based transaction categorization
- **yfinance** + **Frankfurter API** for live FX rates
- **NumPy** for financial simulations (Phase 3)

## Scale

The app is designed to handle **1–10 million transactions**:
- Composite database indexes on (account_id, date), (account_id, amount), category_id, and transfer fields
- SQLite tuned with WAL mode, 64 MB page cache, 256 MB mmap
- Batch flushing during imports (configurable via `IMPORT_BATCH_SIZE`)
- Efficient `COUNT` queries instead of loading full result sets
- PostgreSQL connection pooling available for production deployments
