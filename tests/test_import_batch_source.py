"""import_batches.source must never make a batch unreadable.

The column was a DB enum, so SQLAlchemy resolved the stored text back to a
member on every read. The Revolut PDF importer wrote "revolut_pdf", which was
not a member, and every later load of those rows raised LookupError — the
failure surfaced far from the import that caused it.
"""
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.account import Account, AccountType
from app.models.import_batch import ImportBatch, ImportSource


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _account(db):
    a = Account(name="Card", account_type=AccountType.CREDIT_CARD,
                currency="USD", is_asset=False)
    db.add(a)
    db.flush()
    return a


def _batch(db, account, source):
    b = ImportBatch(account_id=account.id, filename="f.pdf",
                    file_type="pdf", row_count=0, source=source)
    db.add(b)
    db.commit()
    return b.id


def test_every_source_member_round_trips(db):
    acct = _account(db)
    for member in ImportSource:
        bid = _batch(db, acct, member.value)
        db.expire_all()
        assert db.get(ImportBatch, bid).source == member.value


def test_revolut_pdf_source_is_readable(db):
    """The exact value that used to raise on read."""
    acct = _account(db)
    bid = _batch(db, acct, ImportSource.REVOLUT_PDF.value)
    db.expire_all()

    assert db.get(ImportBatch, bid).source == "revolut_pdf"


def test_an_unlisted_source_does_not_break_reads(db):
    """A future writer inventing a value must not poison the table."""
    acct = _account(db)
    bid = _batch(db, acct, "some_new_importer")
    db.expire_all()

    assert db.get(ImportBatch, bid).source == "some_new_importer"
    assert db.execute(select(ImportBatch)).scalars().all()


def test_legacy_member_names_are_folded_to_values(db):
    """init_db's normalisation: the old enum persisted NAMES."""
    acct = _account(db)
    bid = _batch(db, acct, ImportSource.MANUAL_UPLOAD.value)
    db.execute(text("UPDATE import_batches SET source = 'MANUAL_UPLOAD'"))
    db.commit()

    db.execute(text(
        "UPDATE import_batches SET source = LOWER(source) "
        "WHERE source <> LOWER(source)"
    ))
    db.commit()
    db.expire_all()

    assert db.get(ImportBatch, bid).source == "manual_upload"
