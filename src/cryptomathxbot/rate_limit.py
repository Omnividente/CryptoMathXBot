from __future__ import annotations

import time
from collections import deque
from collections.abc import Hashable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: float
    notify: bool


class SlidingWindowLimiter:
    """In-memory request limiter with one rejection notice per window."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window = window_seconds
        self._requests: dict[Hashable, deque[float]] = {}
        self._last_notice: dict[Hashable, float] = {}
        self._last_sweep = 0.0

    def check(self, key: Hashable, *, now: float | None = None) -> RateLimitDecision:
        current = time.monotonic() if now is None else now
        queue = self._requests.setdefault(key, deque())
        cutoff = current - self._window
        while queue and queue[0] <= cutoff:
            queue.popleft()

        if len(queue) < self._max_requests:
            queue.append(current)
            self._maybe_sweep(current)
            return RateLimitDecision(True, 0.0, False)

        retry_after = max(0.0, self._window - (current - queue[0]))
        last_notice = self._last_notice.get(key)
        notify = last_notice is None or current - last_notice >= self._window
        if notify:
            self._last_notice[key] = current
        self._maybe_sweep(current)
        return RateLimitDecision(False, retry_after, notify)

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < max(60.0, self._window):
            return
        self._last_sweep = now
        cutoff = now - self._window
        for key, queue in tuple(self._requests.items()):
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if not queue:
                self._requests.pop(key, None)
        for key, timestamp in tuple(self._last_notice.items()):
            if timestamp <= cutoff:
                self._last_notice.pop(key, None)
