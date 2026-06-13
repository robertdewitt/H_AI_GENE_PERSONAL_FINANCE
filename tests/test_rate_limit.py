"""Sliding-window rate-limiter unit test.

The login flow uses this for both password and passkey auth so a
brute-force attempt against a single user — or against the whole
server — gets cut off at 10 hits per 15 minutes.
"""
import time

from app.services.rate_limit import RateLimiter


def test_first_max_attempts_allowed_then_blocked():
    rl = RateLimiter(max_attempts=3, window_seconds=10)
    assert rl.hit("ip:user") is True
    assert rl.hit("ip:user") is True
    assert rl.hit("ip:user") is True
    assert rl.hit("ip:user") is False
    assert rl.remaining("ip:user") == 0


def test_window_expiry_releases_quota():
    rl = RateLimiter(max_attempts=2, window_seconds=0.1)
    rl.hit("ip:user")
    rl.hit("ip:user")
    assert rl.hit("ip:user") is False
    time.sleep(0.15)
    # Old hits have aged out → quota restored.
    assert rl.hit("ip:user") is True


def test_keys_are_isolated():
    rl = RateLimiter(max_attempts=1, window_seconds=10)
    assert rl.hit("alice") is True
    assert rl.hit("alice") is False
    # Different key — fresh budget.
    assert rl.hit("bob") is True
