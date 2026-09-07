"""One place to call a local text model from.

Exists mainly for one reason: the configured model is a *reasoning* model.
Qwen3.6 streams its chain of thought into a separate ``thinking`` field and
leaves ``response`` empty until it finishes, so a caller that caps
``num_predict`` at 20 — as the categoriser did — burns the whole budget
mid-thought and reads back an empty string. It looks exactly like the model
being unavailable, which is how the categoriser's LLM tier appeared to fail
for months.

Passing ``think=False`` turns that off. Answers arrive in ~0.2s instead of
timing out, which is the difference between usable per-transaction and not.
Models that do not understand the flag get a retry without it.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)


def generate(
    prompt: str,
    *,
    model: str | None = None,
    think: bool = False,
    num_predict: int = 40,
    temperature: float = 0.0,
    response_format: str | None = None,
    timeout: float | None = None,
) -> str | None:
    """Run a prompt and return the text response, or None if unavailable."""
    body: dict = {
        "model": model or settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    if response_format:
        body["format"] = response_format
    if think is not None:
        body["think"] = think

    url = f"{settings.ollama_url}/api/generate"
    wait = timeout if timeout is not None else settings.ollama_timeout

    for attempt in (1, 2):
        try:
            resp = httpx.post(url, json=body, timeout=wait)
            resp.raise_for_status()
            return (resp.json().get("response") or "").strip()
        except httpx.HTTPStatusError as exc:
            # An older model may reject the think flag outright — drop it once.
            if attempt == 1 and "think" in body:
                body.pop("think")
                continue
            log.debug("ollama generate failed: %s", exc)
            return None
        except Exception as exc:
            log.debug("ollama generate failed: %s", exc)
            return None
    return None
