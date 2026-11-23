"""Several files uploaded at once are imported one after another.

Each file gets its usual review page in turn; a file with the same columns
as one just mapped is imported with that mapping without stopping, and the
run ends on a summary of what happened to each file.
"""
import re
from decimal import Decimal
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
import app.services.sessions as sessions_module
from app.config import settings
from app.database import Base, get_db
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.models.user import User
from app.routers import setup as setup_router
from app.services.auth import get_current_user

HEADER = "Post Date,Description,Debit,Credit,Balance\n"


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(username="u", display_name="U", is_admin=True)
    db.add(user); db.flush()
    a = Account(user_id=user.id, name="Checking", account_type=AccountType.CHECKING, currency="USD", is_asset=True)
    b = Account(user_id=user.id, name="Savings", account_type=AccountType.SAVINGS, currency="USD", is_asset=True)
    db.add_all([a, b]); db.commit()

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(setup_router, "needs_setup", lambda db: False)
    monkeypatch.setattr(sessions_module, "lookup_session", lambda db, raw: object())
    main_module.app.dependency_overrides[get_db] = _db
    main_module.app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)
    client = TestClient(main_module.app, follow_redirects=False)
    client.cookies.set("session", "stub-token")
    try:
        yield client, db, a, b
    finally:
        main_module.app.dependency_overrides.clear()
        db.close()


def _form_fields(html: str) -> list[tuple[str, str]]:
    """Hidden fields of the page's confirm form, as the browser would send them."""
    return [(unescape(n), unescape(v)) for n, v in
            re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html)]


def _as_form(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in pairs:
        out.setdefault(k, []).append(v)
    return out


def _mapping_post(html: str) -> dict[str, list[str]]:
    return _as_form(_form_fields(html) + [
        ("col_date", "Post Date"), ("col_description", "Description"),
        ("col_debit", "Debit"), ("col_credit", "Credit"), ("col_balance", "Balance"),
        ("date_format", "mdy"),
    ])


def _rows(db, acct):
    db.expire_all()
    return db.execute(select(Transaction).where(Transaction.account_id == acct.id)).scalars().all()


def test_two_files_with_the_same_columns_need_one_review(env):
    client, db, checking, savings = env
    files = [
        ("files", ("oct.csv", HEADER + "10/3/2026,Mission FCU,3290.00,,1000.00\n", "text/csv")),
        ("files", ("nov.csv", HEADER + "11/3/2026,Mission FCU,3290.00,,2000.00\n"
                                     "11/4/2026,Interest,,1.50,2001.50\n", "text/csv")),
    ]
    first = client.post("/import/upload", files=files,
                        data={"file_account_ids": [str(checking.id), str(savings.id)]})
    assert first.status_code == 200
    assert "File 1 of 2" in first.text and "oct.csv" in first.text

    done = client.post("/import/confirm", data=_mapping_post(first.text))

    assert done.status_code == 200 and "Import finished" in done.text
    assert "2 of 2 files imported" in done.text
    assert "Same columns as the file before" in done.text
    assert [r.amount for r in _rows(db, checking)] == [Decimal("-3290.00")]
    assert sorted(r.amount for r in _rows(db, savings)) == [Decimal("-3290.00"), Decimal("1.50")]


def test_a_skipped_file_is_left_out_and_reported(env):
    client, db, checking, _ = env
    files = [
        ("files", ("a.csv", HEADER + "10/3/2026,Rent,100.00,,1.00\n", "text/csv")),
        ("files", ("b.csv", "Date,Memo,Amount\n10/4/2026,Coffee,-3.00\n", "text/csv")),
    ]
    first = client.post("/import/upload", files=files,
                        data={"file_account_ids": [str(checking.id), str(checking.id)]})
    skipped = client.post("/import/skip", data=_as_form(_form_fields(first.text)))

    # b.csv has different columns, so it stops for its own review.
    assert "File 2 of 2" in skipped.text and "b.csv" in skipped.text
    fields = _form_fields(skipped.text) + [
        ("col_date", "Date"), ("col_description", "Memo"), ("col_amount", "Amount"),
        ("date_format", "mdy"),
    ]
    summary = client.post("/import/confirm", data=_as_form(fields))

    assert "1 of 2 files imported" in summary.text and "Skipped" in summary.text
    assert [r.description for r in _rows(db, checking)] == ["Coffee"]


def test_one_file_still_goes_straight_to_the_account(env):
    client, db, checking, _ = env
    page = client.post("/import/upload", data={"account_id": str(checking.id)},
                       files=[("files", ("a.csv", HEADER + "10/3/2026,Rent,100.00,,1.00\n", "text/csv"))])
    assert "File 1 of" not in page.text

    resp = client.post("/import/confirm", data=_mapping_post(page.text))

    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/accounts/{checking.id}?imported=1")


def test_a_file_for_someone_elses_account_is_refused(env):
    client, db, checking, _ = env
    other = User(username="other", display_name="Other")
    db.add(other); db.flush()
    theirs = Account(user_id=other.id, name="Theirs", account_type=AccountType.CHECKING,
                     currency="USD", is_asset=True)
    db.add(theirs); db.commit()

    resp = client.post("/import/upload", files=[
        ("files", ("a.csv", HEADER, "text/csv")), ("files", ("b.csv", HEADER, "text/csv")),
    ], data={"file_account_ids": [str(checking.id), str(theirs.id)]})

    assert "choose the account this file belongs to" in resp.text


def test_the_account_page_link_preselects_its_account(env):
    client, db, checking, savings = env
    page = client.get(f"/import?account={savings.id}")
    assert f'<option value="{savings.id}" selected>' in page.text
    assert f'<option value="{checking.id}" selected>' not in page.text


def test_a_foreign_account_in_the_link_is_ignored(env):
    client, db, checking, _ = env
    page = client.get("/import?account=9999")
    assert " selected>" not in page.text.split('id="account_id"')[1].split("</select>")[0]
