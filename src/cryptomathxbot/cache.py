from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheHit[T]:
    value: T
    stale: bool


@dataclass(frozen=True, slots=True)
class _Entry[T]:
    value: T
    expires_at: float
    stale_until: float


class TTLCache[T]:
    def __init__(self, max_items: int) -> None:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self._max_items = max_items
        self._items: OrderedDict[str, _Entry[T]] = OrderedDict()

    def get(
        self, key: str, *, allow_stale: bool = False, now: float | None = None
    ) -> CacheHit[T] | None:
        current = time.monotonic() if now is None else now
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.stale_until <= current:
            self._items.pop(key, None)
            return None
        stale = entry.expires_at <= current
        if stale and not allow_stale:
            return None
        self._items.move_to_end(key)
        return CacheHit(entry.value, stale)

    def set(
        self,
        key: str,
        value: T,
        *,
        ttl: float,
        stale_ttl: float = 0.0,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else now
        expires_at = current + max(0.0, ttl)
        self._items[key] = _Entry(value, expires_at, expires_at + max(0.0, stale_ttl))
        self._items.move_to_end(key)
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
