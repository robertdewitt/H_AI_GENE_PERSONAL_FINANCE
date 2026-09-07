"""Embedding-backed text similarity, with a deterministic cache.

Used for comparing transaction descriptions. SequenceMatcher compares
characters, which is the wrong unit for merchant strings: it scores
"TST* KI'S RESTAURANT" against "KIS RESTAURANT LONDON" barely above noise
while scoring unrelated strings that share a prefix far too high.

Three properties this module has to hold:

* **Optional.** Nothing here may raise into a caller. If Ollama is not
  installed — which was true of this machine until recently — every entry
  point falls back to SequenceMatcher and the app behaves exactly as before.
* **Deterministic.** The same text always yields the same vector, because it
  is embedded once and cached. Scores feed duplicate detection and schedule
  matching, so a value that drifts between runs would be worse than a
  slightly worse value that doesn't.
* **Cheap.** Roughly two thousand distinct descriptions exist across the
  whole ledger, so the cache fills once and comparisons are then pure
  arithmetic.
"""
from __future__ import annotations

import json
import logging
import math
import time
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import httpx

from app.config import settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Remember an absent daemon for a while rather than paying a connection
# timeout on every single comparison.
_UNAVAILABLE_FOR = 60.0
_unavailable_until = 0.0


def _normalise(text: str) -> str:
    from app.services.recurring_detector import _normalize

    return _normalize(text or "")


def fallback_similarity(a: str, b: str) -> float:
    """The original character-sequence ratio, on normalised text."""
    raw = SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    norm = SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()
    return max(raw, norm)


def available() -> bool:
    """Whether the embedding model can be reached right now."""
    global _unavailable_until
    if not settings.use_embeddings:
        return False
    if time.monotonic() < _unavailable_until:
        return False
    try:
        resp = httpx.get(f"{settings.ollama_url}/api/tags", timeout=2.0)
        resp.raise_for_status()
        names = {m.get("name", "") for m in resp.json().get("models", [])}
        base = settings.ollama_embed_model.split(":")[0]
        if not any(n == settings.ollama_embed_model or n.startswith(base + ":")
                   or n.split(":")[0] == base for n in names):
            _unavailable_until = time.monotonic() + _UNAVAILABLE_FOR
            return False
        return True
    except Exception:
        _unavailable_until = time.monotonic() + _UNAVAILABLE_FOR
        return False


def _embed_remote(texts: list[str]) -> list[list[float]] | None:
    """Call Ollama for a batch. Returns None on any failure."""
    global _unavailable_until
    if not texts:
        return []
    try:
        resp = httpx.post(
            f"{settings.ollama_url}/api/embed",
            json={"model": settings.ollama_embed_model, "input": texts},
            timeout=settings.ollama_timeout,
        )
        resp.raise_for_status()
        vectors = resp.json().get("embeddings")
        if isinstance(vectors, list) and len(vectors) == len(texts):
            return vectors
    except Exception as exc:
        log.debug("embedding request failed: %s", exc)
    _unavailable_until = time.monotonic() + _UNAVAILABLE_FOR
    return None


def get_vectors(db: "Session", texts: list[str]) -> dict[str, list[float]]:
    """Vectors for the normalised form of each text, cache first.

    Missing entries are embedded in one batch and written to the cache.
    Returns only what could be resolved — callers treat an absent key as
    "no embedding available" and fall back.
    """
    from sqlalchemy import select

    from app.models.description_embedding import DescriptionEmbedding

    keys = {_normalise(t) for t in texts if (t or "").strip()}
    keys.discard("")
    if not keys:
        return {}

    model = settings.ollama_embed_model
    rows = db.execute(
        select(DescriptionEmbedding).where(
            DescriptionEmbedding.model == model,
            DescriptionEmbedding.text_key.in_(list(keys)),
        )
    ).scalars().all()

    out: dict[str, list[float]] = {}
    for row in rows:
        try:
            out[row.text_key] = json.loads(row.vector)
        except (TypeError, ValueError):
            continue

    missing = sorted(keys - set(out))
    if not missing or not available():
        return out

    vectors = _embed_remote(missing)
    if vectors is None:
        return out

    for key, vector in zip(missing, vectors):
        out[key] = vector
        db.add(DescriptionEmbedding(
            model=model, text_key=key[:300], dim=len(vector),
            vector=json.dumps(vector),
        ))
    try:
        db.flush()
    except Exception as exc:            # a concurrent writer won the unique key
        log.debug("embedding cache write skipped: %s", exc)
        db.rollback()
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    # Cosine runs [-1, 1]; clamp to [0, 1] so it drops into the same range
    # every existing threshold was tuned against.
    return max(0.0, min(1.0, dot / (na * nb)))


def similarity(db: "Session", a: str, b: str) -> float:
    """Similarity in [0, 1], embedding-backed when possible."""
    vectors = get_vectors(db, [a, b])
    ka, kb = _normalise(a), _normalise(b)
    if ka in vectors and kb in vectors:
        return cosine(vectors[ka], vectors[kb])
    return fallback_similarity(a, b)


def similarity_matrix(db: "Session", texts: list[str]) -> list[list[float]]:
    """Pairwise similarity for a group, embedding the whole group at once."""
    vectors = get_vectors(db, texts)
    keys = [_normalise(t) for t in texts]
    size = len(texts)
    out = [[1.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            if keys[i] in vectors and keys[j] in vectors:
                score = cosine(vectors[keys[i]], vectors[keys[j]])
            else:
                score = fallback_similarity(texts[i], texts[j])
            out[i][j] = out[j][i] = score
    return out


def warm_cache(db: "Session", limit: int | None = None) -> int:
    """Embed every distinct description on the ledger. Returns rows added."""
    from sqlalchemy import select

    from app.models.transaction import Transaction

    descriptions = [
        d for (d,) in db.execute(select(Transaction.description).distinct()).all()
        if d
    ]
    keys = sorted({_normalise(d) for d in descriptions} - {""})
    if limit:
        keys = keys[:limit]
    before = len(get_vectors(db, keys))
    added = 0
    # Batch so one oversized request cannot fail the whole warm-up.
    for start in range(0, len(keys), 128):
        chunk = keys[start:start + 128]
        got = get_vectors(db, chunk)
        added += len(got)
    return max(0, added - before)
