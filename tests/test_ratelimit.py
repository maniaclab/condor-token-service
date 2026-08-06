"""Unit tests for the per-subject sliding-window rate limiter.

Deterministic: every test injects an explicit clock value rather than
depending on wall time.
"""

from __future__ import annotations

import pytest

from condor_token_service.ratelimit import RateLimiter


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter(max_events=3, window_seconds=300.0)


class TestRateLimiter:
    def test_allows_up_to_max_events(self, limiter: RateLimiter) -> None:
        for i in range(3):
            assert limiter.try_acquire("subject-a", now=float(i)) is None

    def test_blocks_event_over_max_with_retry_after(self, limiter: RateLimiter) -> None:
        for i in range(3):
            limiter.try_acquire("subject-a", now=float(i))
        retry_after = limiter.try_acquire("subject-a", now=10.0)
        # Oldest event was at t=0, window is 300s → a slot frees at t=300.
        assert retry_after == pytest.approx(290.0)

    def test_subjects_are_isolated(self, limiter: RateLimiter) -> None:
        for i in range(3):
            limiter.try_acquire("subject-a", now=float(i))
        assert limiter.try_acquire("subject-b", now=3.0) is None

    def test_window_slides_and_frees_slots(self, limiter: RateLimiter) -> None:
        for i in range(3):
            limiter.try_acquire("subject-a", now=float(i))
        assert limiter.try_acquire("subject-a", now=10.0) is not None
        # At t=300.5 the t=0 event has aged out of the 300s window.
        assert limiter.try_acquire("subject-a", now=300.5) is None
        # But the very next attempt is blocked again (t=1, t=2, t=300.5 held).
        assert limiter.try_acquire("subject-a", now=300.6) is not None

    def test_blocked_attempts_do_not_consume_slots(self, limiter: RateLimiter) -> None:
        for i in range(3):
            limiter.try_acquire("subject-a", now=float(i))
        # Hammering while blocked must not push the retry horizon out.
        for _ in range(10):
            limiter.try_acquire("subject-a", now=5.0)
        assert limiter.try_acquire("subject-a", now=300.5) is None
