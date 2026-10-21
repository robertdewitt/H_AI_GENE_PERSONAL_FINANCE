"""Bulk confirm / dismiss of detected transfer pairs on /transfers."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.models.transfer_link import TransferLink
from tests.test_tenant_isolation import _cookie, two_user_app  # noqa: F401 — fixture


@pytest.fixture
def alice_pairs(two_user_app):
    """Two clean transfer pairs between Alice's checking and savings."""
    client, ctx = two_user_app
    s = ctx["session"]
    savings = Account(
        user_id=s.get(Account, ctx["alice_account_id"]).user_id, name="A-Savings",
        account_type=AccountType.SAVINGS, currency="USD", is_asset=True,
    )
    s.add(savings)
    s.flush()
    pairs = []
    for day, amt in ((3, "500.00"), (10, "275.00")):
        out = Transaction(account_id=ctx["alice_account_id"], date=datetime(2026, 6, day),
                          description="Transfer to savings", amount=-Decimal(amt),
                          original_currency="USD")
        inc = Transaction(account_id=savings.id, date=datetime(2026, 6, day),
                          description="Transfer from checking", amount=Decimal(amt),
                          original_currency="USD")
        s.add_all([out, inc])
        s.flush()
        pairs.append((out.id, inc.id))
    s.commit()
    return client, ctx, pairs


def _post(client, path, cookie, pairs):
    return client.post(
        path, data={"pairs": ",".join(f"{a}:{b}" for a, b in pairs)},
        cookies=_cookie(cookie), follow_redirects=False,
    )


def _links(ctx):
    ctx["session"].expire_all()
    return {(l.from_transaction_id, l.to_transaction_id)
            for l in ctx["session"].query(TransferLink).all()}


def test_page_lists_pairs_before_unmatched_with_checkboxes(alice_pairs):
    client, ctx, pairs = alice_pairs
    html = client.get("/transfers", cookies=_cookie(ctx["alice_cookie"])).text
    assert html.index("Detected Pairs") < html.index("Unmatched Transfers")
    for a, b in pairs:
        assert f'value="{a}:{b}"' in html


def test_bulk_confirm_links_only_selected_pairs(alice_pairs):
    client, ctx, pairs = alice_pairs
    resp = _post(client, "/transfers/bulk-confirm", ctx["alice_cookie"], pairs[:1])
    assert resp.status_code == 303
    assert "linked_count=1" in resp.headers["location"]
    assert _links(ctx) == {pairs[0]}


def test_bulk_confirm_ignores_non_candidates_and_garbage(alice_pairs):
    client, ctx, pairs = alice_pairs
    a, b = pairs[0]
    resp = client.post("/transfers/bulk-confirm",
                       data={"pairs": f"{b}:{a},x:y,{a}"},  # reversed pair is not a candidate
                       cookies=_cookie(ctx["alice_cookie"]), follow_redirects=False)
    assert "linked_count=0" in resp.headers["location"]
    assert _links(ctx) == set()


def test_bulk_dismiss_marks_selected_pairs(alice_pairs):
    client, ctx, pairs = alice_pairs
    _post(client, "/transfers/bulk-dismiss", ctx["alice_cookie"], pairs)
    s = ctx["session"]
    s.expire_all()
    for a, b in pairs:
        assert s.get(Transaction, a).transfer_dismissed
        assert s.get(Transaction, b).transfer_dismissed


def test_other_user_cannot_bulk_act_on_pairs(alice_pairs):
    client, ctx, pairs = alice_pairs
    _post(client, "/transfers/bulk-confirm", ctx["bob_cookie"], pairs)
    _post(client, "/transfers/bulk-dismiss", ctx["bob_cookie"], pairs)
    assert _links(ctx) == set()
    s = ctx["session"]
    s.expire_all()
    assert not any(s.get(Transaction, t).transfer_dismissed for p in pairs for t in p)
