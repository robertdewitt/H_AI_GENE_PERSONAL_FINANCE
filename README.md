# Financial Hygiene — Personal Finance Dashboard

A comprehensive personal finance application that tracks transactions across multiple currencies, detects transfers between accounts, calculates net worth across all asset classes, and provides financial insights.

## Features (Phase 1)

- **Account Management** — Track bank accounts, credit cards, investments, IRAs, pensions, real estate, vehicles, collectibles, and more
- **Multi-Currency / FX Support** — Accounts in any currency; FX rate storage with historical lookups; automatic conversion to base currency for net worth
- **CSV/XLS Import** — Upload transaction files with automatic column detection, manual mapping, and batch processing optimized for 1–10M+ transactions
- **Paycheck Stub Tracking** — Import or manually enter paycheck data with full tax, deduction, and benefit breakdowns
- **Transaction Viewer** — Filter, search, and browse transactions across all accounts with pagination
- **Transfer Detection** — Automatically identify transfers between accounts with confidence scoring
- **Net Worth Tracking** — FX-aware net worth calculation with time-series charts and breakdowns by asset group
- **Asset Valuations** — Manual valuation entry for real estate, vehicles, collectibles, pensions with history tracking and charts
- **Currency Converter** — Convert amounts using stored FX rates

## Upcoming Features

- **Phase 2**: Budgeting, spending analysis, auto-categorization, spending recommendations
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

### Using PostgreSQL (for large-scale datasets)

Set these environment variables (or create a `.env` file):

```bash
DB_BACKEND=postgresql
DATABASE_URL=postgresql://user:password@localhost:5432/financial_hygiene
```

## Workflow

1. **Add accounts** — Create accounts for each bank, brokerage, credit card, property, vehicle, etc.
2. **Upload transaction files** — Import CSV/XLS exports from your banks; columns are auto-detected with manual override
3. **Add FX rates** — If you have non-USD accounts, enter exchange rates under FX Rates
4. **Review transfers** — The system detects transfers between accounts; confirm or dismiss
5. **Value assets** — For real estate, vehicles, and collectibles, add periodic valuations
6. **Upload paychecks** — Track income details including taxes, 401(k), and benefits
7. **View net worth** — See your complete financial picture across all currencies and asset types

## Project Structure

```
app/
├── main.py              # FastAPI app + dashboard route
├── config.py            # Settings (DB backend, base currency, batch size)
├── database.py          # SQLAlchemy setup (SQLite / PostgreSQL)
├── models/              # ORM models (8 tables)
├── schemas/             # Pydantic validation schemas
├── routers/             # Route handlers (8 modules)
├── services/            # Business logic
│   ├── account_service.py      # CRUD + FX-aware balance
│   ├── import_service.py       # Batch CSV/XLS import
│   ├── transfer_detector.py    # Transfer matching algorithm
│   ├── net_worth_service.py    # FX-aware net worth
│   ├── fx_service.py           # Exchange rate management
│   ├── paycheck_service.py     # Paycheck stub parsing
│   └── asset_valuation_service.py  # Asset valuation management
├── templates/           # Jinja2 HTML templates
├── static/              # CSS, JS
└── seeds/               # Default category data
```

## Tech Stack

- **Python 3.11+** with **FastAPI**
- **SQLite** (default) or **PostgreSQL** via **SQLAlchemy** ORM
- **Pico CSS** + **Chart.js** for the UI
- **Pandas** for CSV/XLS parsing (batch-optimized for millions of rows)
- **NumPy** for financial simulations (Phase 3)

## Scale

The app is designed to handle **1–10 million transactions**:
- Composite database indexes on (account_id, date), (account_id, amount), and transfer fields
- SQLite tuned with WAL mode, 64 MB page cache, 256 MB mmap
- Batch flushing during imports (configurable via `IMPORT_BATCH_SIZE`)
- Efficient `COUNT` queries instead of loading full result sets
- PostgreSQL connection pooling available for production deployments
