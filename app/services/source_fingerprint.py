"""Exact identity for a row as it appeared in the source file.

The importer's duplicate check keys on (date, description, amount) taken from
the *parsed* row. That key moves whenever parsing does: a statement re-exported
with different whitespace, a liability whose sign convention is read
differently between two files, a date column that parses DD/MM one time and
MM/DD the next. Each of those makes the same statement line look new, which is
how a re-import lands a second copy of rows that are already there.

Fingerprinting the source row instead sidesteps all of it. The bytes in the
file did not change, so the fingerprint does not either, whatever the parser
later decides the row means.

Two details matter:

* Keys are sorted, so a column reordered in the export does not change the
  fingerprint.
* A repeat index is included, so a statement that genuinely lists the same
  charge twice on the same day keeps both rows — and a re-import still matches
  both. Keying on content alone would silently drop the second one.
"""
from __future__ import annotations

import hashlib
import json


def canonical_row(row: dict) -> str:
    """Stable text for a source row, independent of column order."""
    items = sorted(
        (str(k).strip(), str(v).strip()) for k, v in (row or {}).items()
    )
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def fingerprint(account_id: int, row: dict, repeat: int = 0) -> str:
    """Identity for the ``repeat``-th occurrence of this row on this account."""
    payload = f"{account_id}\x1f{canonical_row(row)}\x1f{repeat}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_from_raw(
    account_id: int, raw_data: str | None, repeat: int = 0,
) -> str | None:
    """Fingerprint a transaction from the source row stored on it at import."""
    if not raw_data:
        return None
    try:
        parsed = json.loads(raw_data)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return fingerprint(account_id, parsed, repeat)


def backfill(db, account_id: int | None = None) -> int:
    """Fingerprint transactions imported before this column existed.

    Without this the exact layer only protects files imported from now on —
    re-importing an older statement would still fall through to the fuzzy key.
    Rows are walked in import order so repeats of an identical source row get
    the same indices they would have been given at the time.

    Returns the number of rows stamped.
    """
    from sqlalchemy import select

    from app.models.transaction import Transaction

    query = select(Transaction).where(
        Transaction.source_hash.is_(None),
        Transaction.raw_data.isnot(None),
    )
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    rows = db.execute(query.order_by(Transaction.id)).scalars().all()

    seen: dict[tuple[int, str], int] = {}
    stamped = 0
    for txn in rows:
        base = fingerprint_from_raw(txn.account_id, txn.raw_data, 0)
        if base is None:
            continue
        key = (txn.account_id, base)
        repeat = seen.get(key, 0)
        seen[key] = repeat + 1
        txn.source_hash = fingerprint_from_raw(
            txn.account_id, txn.raw_data, repeat,
        )
        stamped += 1
    if stamped:
        db.flush()
    return stamped
