from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, replace

from .domain import Calculation


@dataclass(frozen=True, slots=True)
class QuerySession:
    token: str
    owner_user_id: int
    expression: str
    calculation: Calculation
    expires_at: float
    active_timeframe: str | None = None


class QueryRegistry:
    def __init__(self, *, ttl: float = 20 * 60, max_items: int = 1_000) -> None:
        self._ttl = ttl
        self._max_items = max_items
        self._sessions: OrderedDict[str, QuerySession] = OrderedDict()

    def create(self, owner_user_id: int, expression: str, calculation: Calculation) -> QuerySession:
        self._purge()
        token = secrets.token_urlsafe(12)
        while token in self._sessions:
            token = secrets.token_urlsafe(12)
        session = QuerySession(
            token=token,
            owner_user_id=owner_user_id,
            expression=expression,
            calculation=calculation,
            expires_at=time.monotonic() + self._ttl,
        )
        self._sessions[token] = session
        while len(self._sessions) > self._max_items:
            self._sessions.popitem(last=False)
        return session

    def get(self, token: str, owner_user_id: int) -> QuerySession | None:
        self._purge()
        session = self._sessions.get(token)
        if session is None or session.owner_user_id != owner_user_id:
            return None
        self._sessions.move_to_end(token)
        return session

    def update(self, session: QuerySession, calculation: Calculation) -> QuerySession:
        updated = replace(
            session,
            calculation=calculation,
            expires_at=time.monotonic() + self._ttl,
        )
        self._sessions[session.token] = updated
        self._sessions.move_to_end(session.token)
        return updated

    def set_active_timeframe(self, session: QuerySession, timeframe: str) -> QuerySession:
        if timeframe not in {"1h", "24h", "7d"}:
            raise ValueError("unsupported timeframe")
        updated = replace(
            session,
            active_timeframe=timeframe,
            expires_at=time.monotonic() + self._ttl,
        )
        self._sessions[session.token] = updated
        self._sessions.move_to_end(session.token)
        return updated

    def _purge(self) -> None:
        now = time.monotonic()
        for token, session in tuple(self._sessions.items()):
            if session.expires_at <= now:
                self._sessions.pop(token, None)


class ActorLocks:
    def __init__(self, *, ttl: float = 30 * 60) -> None:
        self._ttl = ttl
        self._locks: dict[int, tuple[asyncio.Lock, float]] = {}
        self._last_sweep = 0.0

    def get(self, actor_id: int) -> asyncio.Lock:
        now = time.monotonic()
        current = self._locks.get(actor_id)
        if current is None:
            lock = asyncio.Lock()
        else:
            lock = current[0]
        self._locks[actor_id] = (lock, now)
        if now - self._last_sweep >= self._ttl:
            self._sweep(now)
        return lock

    def _sweep(self, now: float) -> None:
        self._last_sweep = now
        cutoff = now - self._ttl
        for actor_id, (lock, seen_at) in tuple(self._locks.items()):
            if seen_at <= cutoff and not lock.locked():
                self._locks.pop(actor_id, None)
