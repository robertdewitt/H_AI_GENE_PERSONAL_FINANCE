"""Tiny in-memory rate limiter for sensitive endpoints.

Tracks per-key attempts in a fixed sliding window. Designed for login
ceremonies where the call volume is low and a single-process server is
adequate — swap in Redis if you later run multiple workers.
"""
from __future__ import annotations

import threading
from collections import deque
from time import monotonic


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: float):
        self.max = max_attempts
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record an attempt for ``key``. Return False if over the limit."""
        now = monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            # Drop expired entries from the left
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return False
            dq.append(now)
            return True

    def remaining(self, key: str) -> int:
        """How many more attempts ``key`` can make in this window."""
        now = monotonic()
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return self.max
            while dq and now - dq[0] > self.window:
                dq.popleft()
            return max(0, self.max - len(dq))


# Module-level singleton for login flows: 10 attempts per 15 minutes per key
# (key is usually IP + username so a single user typo doesn't lock everyone).
login_limiter = RateLimiter(max_attempts=10, window_seconds=15 * 60)
