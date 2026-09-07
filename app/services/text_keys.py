"""Two different normalisations, because two jobs want opposite things.

Both start from a merchant string, and it is tempting to have one function.
They pull apart on digits:

**Grouping** recurring payments wants digits gone. "STARBUCKS 12428 LONDON"
and "STARBUCKS 12406 LONDON" are the same coffee habit at two branches, and a
detector that treats them separately never reaches the occurrence count that
makes a pattern.

**Comparing** two transactions to ask whether they are the same event wants
digits kept. "Faster Payment IPBINB4001744" and "Faster Payment IPBINB4001743"
are two different payments, and the reference number is the *only* thing that
says so. Stripping digits scores them identical — which is exactly why they
kept surfacing as duplicates, and why they turned up as confirmed
non-duplicates when the embedding work was measured against real decisions.

What both want gone is an embedded date: a statement stamping the due date
into a description ("RegularPayment-(Due06/01/2026)") produces a new string
every month for one unchanging obligation.
"""
from __future__ import annotations

import re

# dd/mm/yyyy, d-m-yy, yyyy-mm-dd — the shapes statements stamp into text.
# Deliberately no \b anchors: statements run the date straight onto a word
# ("RegularPayment-(Due06/01/2026)"), and there is no word boundary between
# "e" and "0", so anchoring left the date in place.
_DATE_RE = re.compile(
    r"(?:\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})"
)
_MONTH_YEAR_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-]*\d{2,4}\b",
    re.IGNORECASE,
)


def comparison_key(description: str | None) -> str:
    """Normalised form for asking whether two rows are the same transaction.

    Dates come out, everything else — including reference numbers — stays.
    """
    text = _DATE_RE.sub(" ", description or "")
    text = _MONTH_YEAR_RE.sub(" ", text)
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def grouping_key(description: str | None) -> str:
    """Normalised form for gathering repeats of one recurring payment.

    Digits come out, so branch numbers and reference codes do not split a
    stream that is really one obligation.
    """
    text = re.sub(r"\d+", "", description or "")
    text = re.sub(r"[^a-zA-Z\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()
