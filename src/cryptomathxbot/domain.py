from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Coin:
    id: str
    symbol: str
    name: str
    market_cap_rank: int | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    coin: Coin
    usd: Decimal
    change_24h: Decimal | None
    source: str
    pair: str | None
    fetched_at: datetime
    stale: bool = False


@dataclass(frozen=True, slots=True)
class Chart:
    symbol: str
    timeframe: str
    points: tuple[tuple[int, float], ...]
    source: str


@dataclass(frozen=True, slots=True)
class Calculation:
    expression: str
    coefficients: dict[str, Decimal]
    constant_usd: Decimal
    quotes: dict[str, Quote]
    total_usd: Decimal
    usd_rub: Decimal | None
    cbr_date: str | None

    @property
    def total_rub(self) -> Decimal | None:
        if self.usd_rub is None:
            return None
        return self.total_usd * self.usd_rub
