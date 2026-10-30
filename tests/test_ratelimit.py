import pytest

from companion.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_max_then_blocks():
    clock = FakeClock()
    limiter = RateLimiter(3, window_seconds=60, clock=clock)
    assert [limiter.try_acquire("u") for _ in range(3)] == [0, 0, 0]
    assert limiter.try_acquire("u") == pytest.approx(60)


def test_reports_time_until_oldest_request_expires():
    clock = FakeClock()
    limiter = RateLimiter(2, window_seconds=60, clock=clock)
    limiter.try_acquire("u")
    clock.now += 20
    limiter.try_acquire("u")
    clock.now += 10
    assert limiter.try_acquire("u") == pytest.approx(30)


def test_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(1, window_seconds=60, clock=clock)
    assert limiter.try_acquire("u") == 0
    clock.now += 59.9
    assert limiter.try_acquire("u") > 0
    clock.now += 0.1
    assert limiter.try_acquire("u") == 0


def test_blocked_attempts_do_not_count():
    clock = FakeClock()
    limiter = RateLimiter(1, window_seconds=60, clock=clock)
    limiter.try_acquire("u")
    for _ in range(5):
        limiter.try_acquire("u")
    clock.now += 60
    assert limiter.try_acquire("u") == 0


def test_users_are_independent():
    limiter = RateLimiter(1, clock=FakeClock())
    assert limiter.try_acquire("a") == 0
    assert limiter.try_acquire("b") == 0
    assert limiter.try_acquire("a") > 0


def test_rejects_invalid_max():
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_retry_after_does_not_record():
    limiter = RateLimiter(1, clock=FakeClock())
    for _ in range(3):
        assert limiter.retry_after("u") == 0
    limiter.record("u")
    assert limiter.retry_after("u") > 0
