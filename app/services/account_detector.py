"""Detect which account a file most likely belongs to.

Scores each account against:
1. Filename tokens
2. First ~2000 chars of file text (PDF first page, CSV header rows)

Returns (account_id, confidence, reason) or (None, 0, "") if no confident match.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _tokenize(text: str) -> set[str]:
    """Lowercase alpha tokens, 3+ chars."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return {t.lower() for t in re.split(r"[^a-zA-Z0-9]+", text) if len(t) >= 3}


def _extract_file_text(path: str, max_chars: int = 2000) -> str:
    """Extract readable text from the first portion of a file."""
    ext = Path(path).suffix.lower()
    try:
        if ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                pages = pdf.pages[:2]
                return " ".join(p.extract_text() or "" for p in pages)[:max_chars]
        elif ext in (".csv",):
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read(max_chars)
        elif ext in (".xls", ".xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            lines = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > 20:
                    break
                lines.append(" ".join(str(c) for c in row if c))
            return " ".join(lines)[:max_chars]
    except Exception:
        pass
    return ""


def _build_account_keywords(account) -> list[str]:
    """Keywords that strongly identify this account."""
    parts = []
    if account.name:
        parts.append(account.name)
    if account.institution:
        parts.append(account.institution)
    return parts


def detect_account(
    filepath: str,
    filename: str,
    accounts: list,
) -> tuple[int | None, float, str]:
    """Return (account_id, confidence 0-1, reason string)."""
    if not accounts:
        return None, 0.0, ""

    file_text = _extract_file_text(filepath)
    combined = f"{filename} {file_text}"
    combined_tokens = _tokenize(combined)

    scores: dict[int, tuple[float, str]] = {}

    for acct in accounts:
        keyword_phrases = _build_account_keywords(acct)
        best_score = 0.0
        best_reason = ""

        for phrase in keyword_phrases:
            phrase_tokens = _tokenize(phrase)
            if not phrase_tokens:
                continue

            # How many of the phrase's tokens appear in the file text?
            matches = phrase_tokens & combined_tokens
            ratio = len(matches) / len(phrase_tokens)

            # Boost if a long phrase token (5+ chars) appears verbatim in text
            verbatim_boost = 0.0
            verbatim_tok: str | None = None
            for tok in phrase_tokens:
                if len(tok) >= 5 and tok in combined.lower():
                    verbatim_boost = 0.2
                    verbatim_tok = tok
                    break

            score = min(1.0, ratio + verbatim_boost)
            if score > best_score:
                best_score = score
                matched = ", ".join(f'"{t}"' for t in sorted(matches)[:3])
                # Source label: filename if any match (or verbatim-boost token)
                # appears in the filename tokens, otherwise file content.
                filename_tokens = _tokenize(filename)
                from_filename = bool(matches & filename_tokens) or (
                    verbatim_tok is not None and verbatim_tok in filename_tokens
                )
                best_reason = (
                    f'matched {matched} in '
                    f'{"filename" if from_filename else "file content"}'
                )

        scores[acct.id] = (best_score, best_reason)

    # Pick the best
    best_id = max(scores, key=lambda k: scores[k][0])
    best_conf, best_reason = scores[best_id]

    # Require at least 0.4 confidence (40% of identifying tokens found)
    if best_conf < 0.4:
        return None, best_conf, ""

    # Penalise if second-best is close (ambiguous)
    sorted_scores = sorted(scores.values(), key=lambda x: -x[0])
    if len(sorted_scores) >= 2 and sorted_scores[1][0] >= best_conf * 0.85:
        return None, best_conf, "ambiguous"

    return best_id, best_conf, best_reason
