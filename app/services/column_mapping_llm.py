"""Ask a local model which column is which, when the heuristics can't tell.

``detect_columns`` matches header text against hint lists — "date", "memo",
"payee". That covers the common exports and misses everything else: a German
or French statement, a bank that calls the description "Booking text", a file
whose amount column is headed with the account number.

The model is asked for a *mapping*, never for parsed data. It names columns;
the existing importer reads the values out of them. Numbers never pass through
it, so a misread digit is not a failure mode that exists here — the worst case
is a wrong column, which is visible on the mapping screen the user confirms
before anything is imported.

Two rules make that safe:

* Every column it names must actually exist in the file. Anything invented is
  discarded rather than passed on.
* It only fills gaps. A field the heuristics already identified is never
  overridden — this is a fallback, not a second opinion.

It answers in delimited lines rather than JSON, for the reason the pension
work established: asked for JSON this model returns good content under
invented keys, and asked to write lines it is exact.
"""
from __future__ import annotations

import logging

from app.config import settings

log = logging.getLogger(__name__)

FIELDS = ("date", "description", "amount", "balance", "currency", "debit", "credit")

_PROMPT = """A bank statement file has these columns:

{columns}

Here are the first rows:

{rows}

Say which column holds each field below. Answer with one line per field,
using | as the separator, and the column name exactly as written above.
Write "none" when the file has no such column.

date | <column>
description | <column>
amount | <column>
balance | <column>
currency | <column>
debit | <column>
credit | <column>

Notes:
- "amount" is a single signed column. If the file instead has separate debit
  and credit columns, set amount to none and fill those two in.
- "description" is the merchant or payee text, not a reference number.
- Output the seven lines and nothing else."""


def _format_rows(rows: list[dict], limit: int = 3) -> str:
    out = []
    for row in rows[:limit]:
        cells = [f"{k}={str(v)[:40]}" for k, v in row.items()]
        out.append("  " + " | ".join(cells))
    return "\n".join(out) or "  (no rows)"


def detect_columns_llm(
    columns: list[str], sample_rows: list[dict],
) -> dict[str, str | None]:
    """Best-effort column mapping. Returns {} when unavailable or unusable."""
    if not columns:
        return {}

    from app.services.ollama_client import generate

    prompt = _PROMPT.format(
        columns="\n".join(f"  - {c}" for c in columns),
        rows=_format_rows(sample_rows),
    )
    answer = generate(
        prompt, think=False, num_predict=200, temperature=0.0,
        timeout=max(settings.ollama_timeout, 30),
    )
    if not answer:
        return {}

    by_lower = {str(c).strip().lower(): c for c in columns}
    mapping: dict[str, str | None] = {}
    for line in answer.splitlines():
        if "|" not in line:
            continue
        field, _, value = line.partition("|")
        field = field.strip().lower()
        value = value.strip()
        if field not in FIELDS:
            continue
        if value.lower() in ("none", "", "-", "null"):
            continue
        # Only accept a column that is really in the file.
        actual = by_lower.get(value.lower())
        if actual is None:
            log.debug("column mapping named a column that is not present: %r", value)
            continue
        mapping[field] = actual
    return mapping


def fill_mapping_gaps(
    mapping: dict, columns: list[str], sample_rows: list[dict],
) -> tuple[dict, list[str]]:
    """Fill only the fields the heuristics left empty.

    Returns the merged mapping and the list of fields the model supplied, so
    the mapping screen can show which choices were not made by the usual
    rules.
    """
    needs_amount = not mapping.get("amount") and not (
        mapping.get("debit") or mapping.get("credit")
    )
    missing = [
        f for f in ("date", "description")
        if not mapping.get(f)
    ]
    if needs_amount:
        missing.append("amount")
    if not missing:
        return mapping, []

    suggested = detect_columns_llm(columns, sample_rows)
    if not suggested:
        return mapping, []

    filled: list[str] = []
    merged = dict(mapping)
    for field, column in suggested.items():
        if merged.get(field):
            continue          # never override what the heuristics decided
        merged[field] = column
        filled.append(field)
    return merged, filled
