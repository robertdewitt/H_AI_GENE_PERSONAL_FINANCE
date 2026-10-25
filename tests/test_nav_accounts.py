"""The Money ▸ Accounts submenu lists the signed-in user's open accounts only."""
from __future__ import annotations

import re

from tests.test_tenant_isolation import _cookie, two_user_app  # noqa: F401 — fixture


def _nav(html: str) -> str:
    return html[html.index('<nav class="container-fluid app-nav">'):html.index("</nav>")]


def test_submenu_lists_own_accounts_only(two_user_app):
    client, ctx = two_user_app
    nav = _nav(client.get("/transactions", cookies=_cookie(ctx["alice_cookie"])).text)
    assert f'href="/accounts/{ctx["alice_account_id"]}"' in nav
    assert "A-Checking" in nav
    assert "B-Checking" not in nav and f'href="/accounts/{ctx["bob_account_id"]}"' not in nav


def test_submenu_is_open_and_marks_current_account(two_user_app):
    client, ctx = two_user_app
    aid = ctx["alice_account_id"]
    nav = _nav(client.get(f"/accounts/{aid}", cookies=_cookie(ctx["alice_cookie"])).text)
    assert re.search(r'class="nav-sub">\s*<details open>', nav)
    assert re.search(rf'href="/accounts/{aid}" aria-current="page"', nav)


def test_closed_accounts_are_left_out(two_user_app):
    from datetime import date
    from app.models.account import Account

    client, ctx = two_user_app
    s = ctx["session"]
    s.get(Account, ctx["alice_account_id"]).closed_at = date(2026, 1, 1)
    s.commit()
    nav = _nav(client.get("/transactions", cookies=_cookie(ctx["alice_cookie"])).text)
    assert "A-Checking" not in nav
