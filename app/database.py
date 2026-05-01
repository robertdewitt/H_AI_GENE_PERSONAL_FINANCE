from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import settings


_connect_args: dict = {}
_pool_kwargs: dict = {}

if settings.db_backend == "sqlite":
    _connect_args = {"check_same_thread": False}
else:
    _pool_kwargs = {
        "poolclass": QueuePool,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
    }

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    echo=settings.debug,
    **_pool_kwargs,
)


if settings.db_backend == "sqlite":
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA cache_size=-64000")  # 64 MB page cache
        cursor.execute("PRAGMA mmap_size=268435456")  # 256 MB mmap
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _add_column_if_missing(
    conn, dialect: str, table: str, column: str, col_type: str,
    default: str | None = None,
) -> None:
    """Idempotent ALTER TABLE ADD COLUMN for SQLite and PostgreSQL.

    SQLite: checks PRAGMA table_info (no native IF NOT EXISTS support).
    PostgreSQL: uses ADD COLUMN IF NOT EXISTS (supported since 9.6).
    Column type strings must be valid for both engines (VARCHAR, REAL,
    BOOLEAN, INTEGER are safe; avoid DATETIME — use TIMESTAMP for PG).
    """
    if dialect == "sqlite":
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        if column in {r[1] for r in rows}:
            return
        ddl = f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
    else:
        # PostgreSQL — DATETIME is not valid; callers should pass TIMESTAMP
        pg_type = col_type.replace("DATETIME", "TIMESTAMP")
        ddl = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {pg_type}"
    if default is not None:
        ddl += f" DEFAULT {default}"
    conn.execute(text(ddl))


def init_db():
    import app.models  # noqa: F401 — ensure models are registered
    Base.metadata.create_all(bind=engine)

    dialect = "sqlite" if settings.db_backend == "sqlite" else "postgresql"

    # Migrations run for ALL backends.
    # CREATE INDEX IF NOT EXISTS and ADD COLUMN IF NOT EXISTS (PostgreSQL 9.6+)
    # are both idempotent, so this block is safe to run on every startup.
    with engine.connect() as conn:
        _col = lambda table, column, col_type, default=None: _add_column_if_missing(
            conn, dialect, table, column, col_type, default
        )

        # ── Truth layer columns on transactions ──────────────
        _col("transactions", "event_type",                 "VARCHAR(50)",  "'unclassified'")
        _col("transactions", "classification_provenance",  "VARCHAR(30)",  "'imported'")
        _col("transactions", "classification_confidence",  "REAL")
        _col("transactions", "financial_document_id",      "INTEGER")
        _col("transactions", "transfer_dismissed",         "BOOLEAN",      "0")

        # ── Truth layer columns on accounts ──────────────────
        _col("accounts", "balance_truth_source",       "VARCHAR(30)",  "'transaction_sum'")
        _col("accounts", "liability_balance_source",   "VARCHAR(40)")
        _col("accounts", "statement_balance",          "REAL")
        _col("accounts", "statement_balance_as_of",    "DATETIME")
        _col("accounts", "original_principal_balance", "REAL")
        _col("accounts", "interest_rate",              "REAL")
        _col("accounts", "monthly_payment",            "REAL")
        _col("accounts", "balance_confidence",         "REAL")
        _col("accounts", "balance_stale_hint",         "BOOLEAN")
        _col("accounts", "liability_balance_stale",    "BOOLEAN")

        # ── Real estate / physical asset fields ──────────────
        _col("accounts", "property_address",          "VARCHAR(500)")
        _col("accounts", "purchase_price",             "REAL")
        _col("accounts", "purchase_date",              "DATETIME")
        _col("accounts", "linked_mortgage_account_id", "INTEGER")

        # ── User profile API keys ─────────────────────────────
        _col("user_profile", "rentcast_api_key",      "VARCHAR(200)")
        _col("user_profile", "property_data_api_key", "VARCHAR(200)")
        _col("user_profile", "domain_api_key",         "VARCHAR(200)")

        # ── v2 recon columns ─────────────────────────────────
        _col("reconciliation_groups", "reconciliation_confidence", "REAL")
        _col("reconciliation_groups", "fx_rate_used",              "REAL")

        # ── v2 split / document columns ──────────────────────
        _col("transaction_splits", "document_line_id", "INTEGER")

        # ── Indexes (CREATE INDEX IF NOT EXISTS works on both backends) ──
        _indexes = [
            ("ix_txn_account_date",   "transactions (account_id, date)"),
            ("ix_txn_account_amount", "transactions (account_id, amount)"),
            ("ix_txn_transfer",       "transactions (is_transfer, transfer_link_id)"),
            ("ix_txn_import_batch",   "transactions (import_batch_id)"),
            ("ix_txn_event_type",     "transactions (event_type)"),
            ("ix_txn_category",       "transactions (category_id)"),
            ("ix_txn_fin_doc",        "transactions (financial_document_id)"),
            ("ix_fx_pair_date",       "currency_rates (base_currency, quote_currency, date)"),
            ("ix_valuation_account_date", "asset_valuations (account_id, date)"),
            ("ix_paycheck_account_date",  "paycheck_stubs (account_id, pay_date)"),
            ("ix_cat_rule_pattern",   "category_rules (pattern)"),
            ("ix_recon_member_txn",   "reconciliation_members (transaction_id)"),
            ("ix_recon_member_group", "reconciliation_members (group_id)"),
            ("ix_payment_decomp_txn", "payment_decompositions (transaction_id)"),
            ("ix_split_txn",          "transaction_splits (transaction_id)"),
            ("ix_split_spend",        "transaction_splits (counts_as_true_spend, spend_type)"),
            ("ix_split_doc_line",     "transaction_splits (document_line_id)"),
            ("ix_acct_bal_snap",      "account_balance_snapshots (account_id, as_of_date)"),
            ("ix_household_snap",     "household_snapshots (as_of_date)"),
            ("ix_fin_doc_type_date",  "financial_documents (document_type, statement_date)"),
            ("ix_fin_doc_prop",       "financial_documents (rental_property_id, statement_date)"),
            ("ix_pnl_prop_period",    "property_pnl_snapshots (rental_property_id, statement_date)"),
            ("ix_instrument_symbol",  "instruments (symbol)"),
            ("ix_position_acct_inst", "position_lots (account_id, instrument_id, as_of_date)"),
            ("ix_price_inst_date",    "price_snapshots (instrument_id, as_of_date)"),
        ]
        for name, spec in _indexes:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {spec}"))

        conn.commit()
