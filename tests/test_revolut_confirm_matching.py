"""A Revolut PDF import runs scheduled-payment matching like any other import."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from tests.test_tenant_isolation import _cookie, two_user_app  # noqa: F401 — fixture


def test_revolut_confirm_matches_scheduled_payments(two_user_app, monkeypatch):
    client, ctx = two_user_app
    s = ctx["session"]
    acct_id = ctx["alice_account_id"]
    calls = []

    def fake_import(db, account_id, filepath, include_sections, account_currency):
        batch = ImportBatch(account_id=account_id, filename="stmt.pdf", file_type="csv", row_count=1)
        db.add(batch)
        db.flush()
        db.add(Transaction(account_id=account_id, date=datetime(2026, 10, 15), description="Metal plan fee",
                           amount=Decimal("-14.99"), original_currency="USD", import_batch_id=batch.id))
        db.commit()
        return batch

    import app.routers.imports as imports_mod
    import app.services.revolut_import as revolut_mod
    import app.services.scheduled_matcher as matcher_mod
    import app.services.upload_safety as safety_mod
    monkeypatch.setattr(revolut_mod, "import_revolut_statement", fake_import)
    monkeypatch.setattr(imports_mod, "revolut_statement_currency", lambda path: None)
    monkeypatch.setattr(safety_mod, "assert_user_owns_path", lambda *a, **k: None)
    monkeypatch.setattr(matcher_mod, "match_batch", lambda db, batch_id: calls.append(("batch", batch_id)))
    monkeypatch.setattr(matcher_mod, "backfill_matches", lambda db, account_id=None, **k: calls.append(("backfill", account_id)))

    resp = client.post("/import/revolut-confirm", data={"account_id": acct_id, "filepath": "x.pdf"},
                       cookies=_cookie(ctx["alice_cookie"]), follow_redirects=False)
    assert resp.status_code == 303
    assert [c[0] for c in calls] == ["batch", "backfill"]
    assert calls[1][1] == acct_id
