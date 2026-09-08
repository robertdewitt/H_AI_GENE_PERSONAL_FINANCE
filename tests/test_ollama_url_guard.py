"""The model endpoint must stay on this machine unless deliberately overridden.

Everything the app sends to the local model — transaction descriptions and
amounts, column-mapping samples, full statement page images — crosses
ollama_url. It is read from .env with no other check, so one edited line
would ship the ledger to a remote host with nothing else changing. The
validator makes that an explicit two-step decision.
"""
import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**overrides):
    # _env_file=None: the machine's own .env must not leak into the test.
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("url", [
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "http://[::1]:11434",
    "http://LOCALHOST:11434",
    "https://localhost:11434",
])
def test_loopback_urls_are_accepted(url):
    assert _settings(ollama_url=url).ollama_url == url


@pytest.mark.parametrize("url", [
    "http://192.168.1.50:11434",
    "http://ollama.internal:11434",
    "https://api.example.com/v1",
    "http://127.0.0.1.evil.example:11434",
])
def test_remote_urls_are_rejected_by_default(url):
    with pytest.raises(ValidationError) as excinfo:
        _settings(ollama_url=url)

    assert "not this machine" in str(excinfo.value)


def test_remote_is_allowed_only_with_the_explicit_flag():
    s = _settings(ollama_url="http://192.168.1.50:11434", ollama_allow_remote=True)

    assert s.ollama_url == "http://192.168.1.50:11434"


def test_the_flag_defaults_off():
    assert _settings().ollama_allow_remote is False


def test_the_default_url_is_loopback():
    assert _settings().ollama_url == "http://localhost:11434"
