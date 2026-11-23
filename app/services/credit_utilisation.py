"""How much of the available card credit is in use.

Utilisation is the amount owed on cards over their combined credit limits.
Only cards with a known limit count, on both sides: a charge card with no
preset limit (Amex Platinum) or a card whose limit was never captured would
otherwise inflate the percentage. A card in credit owes nothing, so it adds
its limit but no usage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.account import Account, AccountType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass
class CardUse:
    account: Account
    owed: Decimal          # in the currency the caller passed balances in
    limit: Decimal | None
    pct: float | None


@dataclass
class Utilisation:
    cards: list[CardUse] = field(default_factory=list)
    total_owed: Decimal = Decimal("0.00")
    total_limit: Decimal = Decimal("0.00")
    no_limit: list[Account] = field(default_factory=list)

    @property
    def pct(self) -> float | None:
        if self.total_limit <= 0:
            return None
        return float(self.total_owed / self.total_limit * 100)

    @property
    def available(self) -> Decimal:
        return self.total_limit - self.total_owed


def card_use(account: Account, balance, limit=None) -> CardUse:
    """One card's usage. ``balance`` is the amount owed (negative = in
    credit); ``limit`` defaults to the card's own, in the card's currency."""
    owed = max(Decimal(str(balance or 0)), Decimal("0.00"))
    lim = Decimal(str(limit)) if limit is not None else (
        Decimal(str(account.credit_limit)) if account.credit_limit else None
    )
    pct = float(owed / lim * 100) if lim and lim > 0 else None
    return CardUse(account=account, owed=owed, limit=lim, pct=pct)


def household_utilisation(
    db: "Session", items: list[tuple[Account, Decimal]], currency: str,
) -> Utilisation:
    """Utilisation across open cards. ``items`` are (account, balance) with
    balances already in ``currency``; limits are converted to match."""
    from app.services.clock import naive_utc_now
    from app.services.fx_service import convert_amount

    out = Utilisation()
    now = naive_utc_now()
    for acct, balance in items:
        if acct.account_type != AccountType.CREDIT_CARD or acct.closed_at is not None:
            continue
        if not acct.credit_limit:
            out.no_limit.append(acct)
            continue
        limit = Decimal(str(acct.credit_limit))
        if (acct.currency or currency).upper() != currency.upper():
            converted, _rate = convert_amount(db, limit, acct.currency, currency, now)
            if converted is None:
                out.no_limit.append(acct)
                continue
            limit = Decimal(str(converted))
        use = card_use(acct, balance, limit)
        out.cards.append(use)
        out.total_owed += use.owed
        out.total_limit += limit
    return out
