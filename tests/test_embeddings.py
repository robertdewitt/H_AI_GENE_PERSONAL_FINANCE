"""Embedding-backed description similarity.

The property that matters most is that this is *optional*: the machine had no
local model at all until recently, and the app has to behave identically when
the daemon is missing. The second is determinism — these scores feed duplicate
detection, so a value that drifts between runs is worse than a slightly worse
value that doesn't.
"""
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.description_embedding import DescriptionEmbedding
from app.services import embeddings as emb


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _reset_availability_cache():
    emb._unavailable_until = 0.0
    yield
    emb._unavailable_until = 0.0


def _stub_model(monkeypatch, vectors: dict[str, list[float]]):
    """Pretend a local model exists and returns these vectors."""
    monkeypatch.setattr(emb, "available", lambda: True)

    def _embed(texts):
        return [vectors[t] for t in texts]

    monkeypatch.setattr(emb, "_embed_remote", _embed)


# ── Degrading without a model ────────────────────────────────────────────


def test_falls_back_when_no_daemon_is_reachable(db, monkeypatch):
    monkeypatch.setattr(emb, "available", lambda: False)

    score = emb.similarity(db, "SPOTIFY UK LONDON", "SPOTIFY UK      LONDON")

    assert score == pytest.approx(
        emb.fallback_similarity("SPOTIFY UK LONDON", "SPOTIFY UK      LONDON")
    )


def test_a_failing_request_does_not_raise(db, monkeypatch):
    monkeypatch.setattr(emb, "available", lambda: True)
    monkeypatch.setattr(emb, "_embed_remote", lambda texts: None)

    assert 0.0 <= emb.similarity(db, "TESCO", "TESCO EXPRESS") <= 1.0


def test_disabled_by_configuration(monkeypatch):
    monkeypatch.setattr(emb.settings, "use_embeddings", False)

    assert emb.available() is False


# ── Caching ──────────────────────────────────────────────────────────────


def test_vectors_are_cached_and_not_re_requested(db, monkeypatch):
    calls = []
    monkeypatch.setattr(emb, "available", lambda: True)

    def _embed(texts):
        calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(emb, "_embed_remote", _embed)

    emb.get_vectors(db, ["OCADO HATFIELD"])
    db.commit()
    emb.get_vectors(db, ["OCADO HATFIELD"])

    assert len(calls) == 1
    assert db.execute(select(DescriptionEmbedding)).scalars().all()


def test_cache_is_keyed_on_the_normalised_text(db, monkeypatch):
    """Two spellings that normalise the same must share one vector."""
    monkeypatch.setattr(emb, "available", lambda: True)
    monkeypatch.setattr(emb, "_embed_remote", lambda texts: [[1.0, 0.0]] * len(texts))

    emb.get_vectors(db, ["SPOTIFY UK      LONDON"])
    db.commit()
    emb.get_vectors(db, ["spotify uk london"])

    rows = db.execute(select(DescriptionEmbedding)).scalars().all()
    assert len(rows) == 1


def test_a_cached_vector_survives_the_model_being_unreachable(db, monkeypatch):
    monkeypatch.setattr(emb, "available", lambda: True)
    monkeypatch.setattr(emb, "_embed_remote", lambda texts: [[0.0, 1.0]] * len(texts))
    emb.get_vectors(db, ["TFL TRAVEL CHARGE"])
    db.commit()

    monkeypatch.setattr(emb, "available", lambda: False)
    got = emb.get_vectors(db, ["TFL TRAVEL CHARGE"])

    assert got and list(got.values())[0] == [0.0, 1.0]


def test_scores_are_stable_across_calls(db, monkeypatch):
    _stub_model(monkeypatch, {"tesco express": [1.0, 0.0], "tesco metro": [0.9, 0.1]})

    first = emb.similarity(db, "TESCO EXPRESS", "TESCO METRO")
    db.commit()
    second = emb.similarity(db, "TESCO EXPRESS", "TESCO METRO")

    assert first == second


# ── The arithmetic ───────────────────────────────────────────────────────


def test_cosine_range_and_clamping():
    assert emb.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert emb.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # Opposed vectors clamp to 0 rather than going negative, so the score
    # stays in the range every existing threshold was tuned against.
    assert emb.cosine([1.0, 0.0], [-1.0, 0.0]) == 0.0
    assert emb.cosine([], [1.0]) == 0.0


def test_similarity_uses_the_vectors_when_present(db, monkeypatch):
    _stub_model(monkeypatch, {"a shop": [1.0, 0.0], "b shop": [0.0, 1.0]})

    # Character-wise these are near-identical; as vectors they are opposed.
    assert emb.fallback_similarity("A SHOP", "B SHOP") > 0.8
    assert emb.similarity(db, "A SHOP", "B SHOP") == pytest.approx(0.0)


def test_group_matrix_is_symmetric_with_a_unit_diagonal(db, monkeypatch):
    _stub_model(monkeypatch, {
        "one": [1.0, 0.0], "two": [0.0, 1.0], "three": [1.0, 1.0],
    })

    m = emb.similarity_matrix(db, ["ONE", "TWO", "THREE"])

    assert all(m[i][i] == 1.0 for i in range(3))
    assert all(m[i][j] == m[j][i] for i in range(3) for j in range(3))


def test_duplicate_detector_still_works_without_a_model(db, monkeypatch):
    """The detector's own entry point, with no daemon anywhere."""
    from app.services.duplicate_detector import find_duplicate_groups

    monkeypatch.setattr(emb, "available", lambda: False)

    assert find_duplicate_groups(db) == []
