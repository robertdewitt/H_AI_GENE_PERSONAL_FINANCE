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


def init_db():
    import app.models  # noqa: F401 — ensure models are registered
    Base.metadata.create_all(bind=engine)

    if settings.db_backend == "sqlite":
        with engine.connect() as conn:
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
            conn.commit()
