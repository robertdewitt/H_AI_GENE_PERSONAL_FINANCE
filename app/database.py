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


def _sqlite_add_column_if_missing(
    conn, table: str, column: str, col_type: str, default: str | None = None,
):
    """Idempotent ALTER TABLE ADD COLUMN for SQLite migrations."""
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    existing = {r[1] for r in rows}
    if column not in existing:
        ddl = f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
        if default is not None:
            ddl += f" DEFAULT {default}"
        conn.execute(text(ddl))


def init_db():
    import app.models  # noqa: F401 — ensure models are registered
    Base.metadata.create_all(bind=engine)

    if settings.db_backend == "sqlite":
        with engine.connect() as conn:
            # ── Truth layer columns on transactions ──────────
            _sqlite_add_column_if_missing(
                conn, "transactions", "event_type",
                "VARCHAR(50)", "'unclassified'",
            )
            _sqlite_add_column_if_missing(
                conn, "transactions", "classification_provenance",
                "VARCHAR(30)", "'imported'",
            )
            _sqlite_add_column_if_missing(
                conn, "transactions", "classification_confidence",
                "REAL",
            )

            # ── Truth layer columns on accounts ──────────────
            _sqlite_add_column_if_missing(
                conn, "accounts", "balance_truth_source",
                "VARCHAR(30)", "'transaction_sum'",
            )
            _sqlite_add_column_if_missing(
                conn, "accounts", "liability_balance_source",
                "VARCHAR(40)",
            )
            _sqlite_add_column_if_missing(
                conn, "accounts", "statement_balance", "REAL",
            )
            _sqlite_add_column_if_missing(
                conn, "accounts", "statement_balance_as_of", "DATETIME",
            )
            _sqlite_add_column_if_missing(
                conn, "accounts", "original_principal_balance", "REAL",
            )
            _sqlite_add_column_if_missing(
                conn, "accounts", "balance_confidence", "REAL",
            )
            _sqlite_add_column_if_missing(
                conn, "accounts", "balance_stale_hint", "BOOLEAN",
            )
            _sqlite_add_column_if_missing(
                conn, "accounts", "liability_balance_stale", "BOOLEAN",
            )

            # ── Indexes ──────────────────────────────────────
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_txn_account_date "
                "ON transactions (account_id, date)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_txn_account_amount "
                "ON transactions (account_id, amount)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_txn_transfer "
                "ON transactions (is_transfer, transfer_link_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_txn_import_batch "
                "ON transactions (import_batch_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_txn_event_type "
                "ON transactions (event_type)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_fx_pair_date "
                "ON currency_rates (base_currency, quote_currency, date)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_valuation_account_date "
                "ON asset_valuations (account_id, date)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_paycheck_account_date "
                "ON paycheck_stubs (account_id, pay_date)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_txn_category "
                "ON transactions (category_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_cat_rule_pattern "
                "ON category_rules (pattern)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_recon_member_txn "
                "ON reconciliation_members (transaction_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_recon_member_group "
                "ON reconciliation_members (group_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_payment_decomp_txn "
                "ON payment_decompositions (transaction_id)"
            ))
            conn.commit()
