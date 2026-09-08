"""Shared test setup.

Embeddings are switched off for the suite by default. They are optional
everywhere in the app — every caller falls back to the sequence ratio when no
model is reachable — so leaving them on would make the tests depend on whether
a local daemon happens to be running, which is both slow and a source of
results that differ between this machine and anywhere else.

Tests that are specifically about embeddings stub the transport directly and
are unaffected by this.
"""
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _no_live_models(monkeypatch):
    monkeypatch.setattr(settings, "use_embeddings", False)
    yield
