from cryptomathxbot.cache import TTLCache
from cryptomathxbot.rate_limit import SlidingWindowLimiter


def test_cache_supports_stale_if_error_window() -> None:
    cache: TTLCache[str] = TTLCache(max_items=2)
    cache.set("btc", "value", ttl=10, stale_ttl=20, now=100)

    assert cache.get("btc", now=105).value == "value"  # type: ignore[union-attr]
    assert cache.get("btc", now=111) is None
    stale = cache.get("btc", allow_stale=True, now=111)
    assert stale is not None and stale.value == "value" and stale.stale
    assert cache.get("btc", allow_stale=True, now=131) is None


def test_cache_evicts_least_recently_used_item() -> None:
    cache: TTLCache[int] = TTLCache(max_items=2)
    cache.set("a", 1, ttl=100, now=0)
    cache.set("b", 2, ttl=100, now=0)
    assert cache.get("a", now=1) is not None
    cache.set("c", 3, ttl=100, now=1)

    assert cache.get("b", now=1) is None
    assert cache.get("a", now=1) is not None
    assert cache.get("c", now=1) is not None


def test_rate_limit_sends_only_one_rejection_notice_per_window() -> None:
    limiter = SlidingWindowLimiter(max_requests=2, window_seconds=10)

    assert limiter.check("user", now=0).allowed
    assert limiter.check("user", now=1).allowed
    first_rejection = limiter.check("user", now=2)
    second_rejection = limiter.check("user", now=3)

    assert not first_rejection.allowed and first_rejection.notify
    assert not second_rejection.allowed and not second_rejection.notify
    assert first_rejection.retry_after == 8
    assert limiter.check("user", now=11).allowed
