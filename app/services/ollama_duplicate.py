"""Ollama-powered duplicate transaction scorer.

Uses a local LLM to decide whether two bank transaction descriptions
refer to the same real-world transaction, with few-shot examples derived
from the user's own history (confirmed duplicates = deleted pairs;
confirmed non-duplicates = dismissed groups).

Falls back gracefully to None when Ollama is unavailable.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"
OLLAMA_TIMEOUT = 10  # seconds per group


# ── Few-shot example mining ──────────────────────────────────────────────────

def _get_examples(db: "Session") -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (confirmed_dupes, confirmed_non_dupes) description pairs.

    confirmed_dupes:  deleted transactions that share (account, date, amount)
                      with an existing transaction → user deleted one copy
    confirmed_non_dupes: dismissed duplicate groups → user said "not a duplicate"
    """
    from sqlalchemy import select, func as _func
    from app.models.deleted_transaction import DeletedTransaction
    from app.models.transaction import Transaction
    from app.models.dismissed_duplicate import DismissedDuplicate

    dupes: list[tuple[str, str]] = []
    non_dupes: list[tuple[str, str]] = []

    # ── confirmed dupes: deleted rows that match a surviving transaction ──
    deleted = db.execute(
        select(DeletedTransaction)
        .where(DeletedTransaction.description.isnot(None))
        .order_by(DeletedTransaction.deleted_at.desc())
        .limit(100)
    ).scalars().all()

    for d in deleted:
        date_val = d.date.date() if hasattr(d.date, "date") else d.date
        surviving = db.execute(
            select(Transaction)
            .where(
                Transaction.account_id == d.account_id,
                _func.date(Transaction.date) == str(date_val),
                Transaction.amount == d.amount,
                Transaction.description.isnot(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        if surviving and surviving.description:
            dupes.append((d.description or "", surviving.description))
            if len(dupes) >= 5:
                break

    # ── confirmed non-dupes: dismissed group pairs ──
    dismissed = db.execute(select(DismissedDuplicate).limit(20)).scalars().all()
    for d in dismissed:
        txns = db.execute(
            select(Transaction)
            .where(
                Transaction.account_id == d.account_id,
                _func.date(Transaction.date) == d.txn_date,
                Transaction.amount == d.amount,
                Transaction.description.isnot(None),
            )
            .limit(2)
        ).scalars().all()
        if len(txns) >= 2:
            non_dupes.append((txns[0].description or "", txns[1].description or ""))
            if len(non_dupes) >= 5:
                break

    return dupes, non_dupes


# ── Prompt builder ───────────────────────────────────────────────────────────

def _build_prompt(
    descriptions: list[str],
    dupes: list[tuple[str, str]],
    non_dupes: list[tuple[str, str]],
) -> str:
    lines = [
        "You are a bank transaction de-duplication assistant.",
        "Decide if a group of transaction descriptions all refer to the SAME real-world",
        "financial event (e.g. the same purchase imported from two different bank files).",
        "Reply with a single JSON object: {\"is_duplicate\": true/false, \"confidence\": 0.0-1.0}",
        "Do not output anything else.",
        "",
    ]

    if dupes:
        lines.append("Examples of CONFIRMED duplicates (same event, different descriptions):")
        for a, b in dupes:
            lines.append(f'  "{a}" ↔ "{b}"')
        lines.append("")

    if non_dupes:
        lines.append("Examples of CONFIRMED non-duplicates (different events):")
        for a, b in non_dupes:
            lines.append(f'  "{a}" ↔ "{b}"')
        lines.append("")

    lines.append("Now evaluate this group:")
    for i, d in enumerate(descriptions, 1):
        lines.append(f'  {i}. "{d}"')
    lines.append("")
    lines.append('Reply with JSON only: {"is_duplicate": ..., "confidence": ...}')

    return "\n".join(lines)


# ── Main scorer ──────────────────────────────────────────────────────────────

class OllamaTransportError(Exception):
    """Connection / HTTP error talking to Ollama — service is unreachable."""


class OllamaContentError(Exception):
    """Ollama responded but the body was unusable (malformed JSON, missing keys)."""


def score_group(
    descriptions: list[str],
    dupes: list[tuple[str, str]],
    non_dupes: list[tuple[str, str]],
) -> tuple[bool, float]:
    """Ask Ollama whether this group is a real duplicate.

    Raises:
        OllamaTransportError — connection failed / HTTP error. Caller should
            mark Ollama unreachable and skip subsequent calls.
        OllamaContentError — response was malformed for this group only.
            Caller should skip this group but keep trying others.
    """
    import json

    prompt = _build_prompt(descriptions, dupes, non_dupes)
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0, "num_predict": 40},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        raise OllamaTransportError(str(exc)) from exc

    try:
        raw = resp.json().get("response", "").strip()
        data = json.loads(raw)
        is_dup = bool(data.get("is_duplicate", False))
        conf = float(data.get("confidence", 0.5))
        conf = max(0.0, min(1.0, conf))
        return is_dup, conf
    except (ValueError, KeyError, TypeError) as exc:
        raise OllamaContentError(str(exc)) from exc


def score_groups_with_db(
    groups: list,  # list[DuplicateGroup]
    db: "Session",
) -> None:
    """Mutate each group in-place, setting ollama_score and ollama_suggested.

    Cross-batch groups are always suggested (different import files = real duplicate).
    Same-batch groups use Ollama when available; identical descriptions are auto-suggested.
    Skips Ollama if unreachable (first failure → skip rest).
    """
    dupes, non_dupes = _get_examples(db)

    reachable: bool | None = None  # None = untested yet

    for group in groups:
        descs = [t.description or "" for t in group.transactions]

        # Cross-batch: different files importing the same transaction — suggest by default
        if group.cross_batch:
            group.ollama_suggested = True
            # Still try Ollama for a confidence score, but don't block on it
            if reachable is not False:
                try:
                    _is_dup, conf = score_group(descs, dupes, non_dupes)
                    reachable = True
                    group.ollama_score = conf
                except OllamaTransportError:
                    reachable = False
                except OllamaContentError:
                    pass  # skip just this group; Ollama is still up
            continue

        # Same-batch: identical descriptions → trivially suggested
        if len(set(descs)) == 1:
            group.ollama_score = 1.0
            group.ollama_suggested = True
            continue

        # Same-batch, different descriptions → ask Ollama
        if reachable is False:
            continue
        try:
            is_dup, conf = score_group(descs, dupes, non_dupes)
        except OllamaTransportError:
            reachable = False
            continue
        except OllamaContentError:
            continue  # skip this group only
        reachable = True
        group.ollama_score = conf
        group.ollama_suggested = is_dup and conf >= 0.65
