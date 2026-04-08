# Financial Hygiene — Personal Finance Dashboard

A comprehensive personal finance application that tracks transactions across multiple currencies, detects transfers between accounts, calculates net worth across all asset classes, and provides financial insights with auto-categorization.

## Features

- **Account Management** — Track bank accounts, credit cards, investments, IRAs, pensions, real estate, vehicles, collectibles, and more
- **Multi-Currency / FX Support** — Accounts in any currency with proper currency symbols (£, €, ¥, etc.); live rate fetching from Yahoo Finance and ECB; automatic conversion to base currency for net worth
- **FX Rate Bootstrap** — On startup, automatically fetches 5 years of daily historical rates for USD/GBP, USD/EUR, and USD/JPY; patches missing dates via yearly-chunked API calls
- **CSV/XLS Import** — Upload transaction files with automatic column detection, manual mapping, and batch processing optimized for 1–10M+ transactions
- **Liability Sign-Flip** — Credit card charges, loan payments, and mortgage transactions are automatically sign-corrected on import so balances reflect money owed
- **Transaction CRUD** — Edit or delete any individual transaction; bulk select multiple transactions to set category, mark as transfer, or delete
- **Auto-Categorization** — Three-tier engine that categorizes transactions automatically:
  1. **Learned rules** — When you correct a category, the system saves the pattern and retroactively updates all matching transactions
  2. **Keyword heuristics** — Built-in mappings for common merchants (groceries, dining, gas, subscriptions, etc.)
  3. **Ollama LLM** — Falls back to a local, free LLM (llama3.2 via [Ollama](https://ollama.com)) for anything the rules and keywords miss
- **Category Management** — Add, edit, and delete categories; view transaction counts per category; manage learned rules with hit counts
- **Spending Summaries** — Category breakdown tables and charts on both account detail pages and the net worth page; amounts displayed in native account currency
- **Transfer Detection** — Detects transfers including payments, credits, ACH, and autopay; confidence scoring with bulk-confirm threshold; expanded keyword matching for liability payments
- **Net Worth Tracking** — FX-aware net worth calculation with time-series charts, asset group breakdowns, and spending-by-category bar chart
- **Paycheck Stub Tracking** — Import or manually enter paycheck data with full tax, deduction, and benefit breakdowns
- **Asset Valuations** — Manual valuation entry for real estate, vehicles, collectibles, pensions with history tracking and charts
- **Currency Converter** — Convert amounts using stored FX rates

## Upcoming Features

- **Phase 2**: Budgeting, spending recommendations, recurring transaction detection
- **Phase 3**: Retirement planning with Monte Carlo simulations
- **Phase 4**: Automated bank imports (Plaid API), alerts, scheduled refresh

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
2. **Upload transaction files** — Import CSV/XLS exports from your banks; columns are auto-detected with manual override. Transactions are auto-categorized on import.
3. **Review & edit transactions** — Edit individual transactions or bulk-select to set categories, mark transfers, or delete. Category corrections teach the system for future imports.
4. **Manage categories** — Add/edit/delete categories at `/categories`; view learned rules and their hit counts.
5. **FX rates load automatically** — On startup, 5 years of daily rates for GBP, EUR, and JPY are fetched. Additional pairs can be fetched or entered manually under FX Rates.
6. **Review transfers** — The system detects transfers (including payments and credits) between accounts; confirm individually or bulk-confirm above a confidence threshold.
7. **Value assets** — For real estate, vehicles, and collectibles, add periodic valuations.
8. **Upload paychecks** — Track income details including taxes, 401(k), and benefits.
9. **View net worth** — See your complete financial picture across all currencies and asset types, with spending breakdowns by category.

## Project Structure

```
app/
├── main.py              # FastAPI app + dashboard route
├── config.py            # Settings (DB backend, base currency, batch size)
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
├── routers/             # Route handlers (9 modules)
│   ├── accounts.py      # Account CRUD + detail with spending summary
│   ├── transactions.py  # Transaction CRUD, bulk edit, auto-categorize
│   ├── categories.py    # Category management + learned rules
│   ├── imports.py       # CSV/XLS upload with auto-categorize on import
│   ├── transfers.py     # Transfer detection and management
│   ├── net_worth.py     # Net worth + category spending summary
│   ├── valuations.py    # Asset valuation management
│   ├── paychecks.py     # Paycheck stub management
│   └── fx.py            # FX rates, live fetching, converter
├── services/            # Business logic
│   ├── account_service.py          # CRUD + FX-aware balance
│   ├── import_service.py           # Batch CSV/XLS import with liability sign-flip
│   ├── categorizer.py              # Auto-categorization (rules + keywords + Ollama)
│   ├── transfer_detector.py        # Transfer matching algorithm
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
