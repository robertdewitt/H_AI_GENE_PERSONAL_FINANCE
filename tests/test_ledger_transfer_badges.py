"""Ledger rows show which account a transfer matched with, or flag it unmatched."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.services.transfer_detector import link_transfer
from tests.test_tenant_isolation import _cookie, two_user_app  # noqa: F401 — fixture


def _txn(s, acct_id, day, amount, desc, is_transfer=False):
    t = Transaction(account_id=acct_id, date=datetime(2026, 10, day), description=desc,
                    amount=Decimal(amount), original_currency="USD", is_transfer=is_transfer)
    s.add(t)
    s.flush()
    return t


def _setup(ctx):
    s = ctx["session"]
    checking = s.get(Account, ctx["alice_account_id"])
    savings = Account(user_id=checking.user_id, name="A-Savings", account_type=AccountType.SAVINGS,
                      currency="USD", is_asset=True)
    s.add(savings)
    s.flush()
    out = _txn(s, checking.id, 3, "-250.00", "Move to savings")
    inc = _txn(s, savings.id, 3, "250.00", "From checking")
    lone = _txn(s, checking.id, 4, "-99.00", "Mystery transfer", is_transfer=True)
    s.commit()
    link_transfer(s, out.id, inc.id, confirmed=True)
    s.commit()
    return checking, savings, out, inc, lone


def _row(html, desc):
    i = html.index(desc)
    return html[i:html.index("</tr>", i)]


def test_account_ledger_names_the_matched_account_and_flags_unmatched(two_user_app):
    client, ctx = two_user_app
    checking, savings, *_ = _setup(ctx)

    html = client.get(f"/accounts/{checking.id}", cookies=_cookie(ctx["alice_cookie"])).text
    assert f'href="/accounts/{savings.id}"' in _row(html, "Move to savings")
    assert "→ A-Savings" in _row(html, "Move to savings")
    assert "Unmatched transfer" in _row(html, "Mystery transfer")

    html = client.get(f"/accounts/{savings.id}", cookies=_cookie(ctx["alice_cookie"])).text
    assert "← A-Checking" in _row(html, "From checking")


def test_transactions_list_shows_the_same_badges(two_user_app):
    client, ctx = two_user_app
    _setup(ctx)
    html = client.get("/transactions", cookies=_cookie(ctx["alice_cookie"])).text
    assert "→ A-Savings" in _row(html, "Move to savings")
    assert "Unmatched transfer" in _row(html, "Mystery transfer")


def test_another_users_account_is_never_named(two_user_app):
    client, ctx = two_user_app
    s = ctx["session"]
    mine = _txn(s, ctx["alice_account_id"], 5, "-10.00", "Odd link")
    theirs = _txn(s, ctx["bob_account_id"], 5, "10.00", "Bob side")
    s.commit()
    link_transfer(s, mine.id, theirs.id, confirmed=True)   # bad data, not reachable via the UI
    s.commit()

    html = client.get(f"/accounts/{ctx['alice_account_id']}", cookies=_cookie(ctx["alice_cookie"])).text
    assert "B-Checking" not in html


def test_transfer_category_without_a_link_is_flagged_unmatched(two_user_app):
    from app.models.category import Category, CategoryType

    client, ctx = two_user_app
    s = ctx["session"]
    user_id = s.get(Account, ctx["alice_account_id"]).user_id
    cat = Category(name="Account Transfer", category_type=CategoryType.TRANSFER, user_id=user_id)
    s.add(cat)
    s.flush()
    t = _txn(s, ctx["alice_account_id"], 6, "-5043.00", "Electronic payment American Express")
    t.category_id = cat.id
    s.commit()

    html = client.get(f"/accounts/{ctx['alice_account_id']}", cookies=_cookie(ctx["alice_cookie"])).text
    assert "Unmatched transfer" in _row(html, "Electronic payment American Express")
