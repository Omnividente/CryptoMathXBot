from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import xml.etree.ElementTree as StdET
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx
from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from .cache import TTLCache
from .config import Settings
from .domain import Chart, Coin, Quote

_LOGGER = logging.getLogger(__name__)
_BINANCE = "https://api.binance.com"
_KUCOIN = "https://api.kucoin.com"
_COINGECKO = "https://api.coingecko.com/api/v3"
_COINPAPRIKA = "https://api.coinpaprika.com/v1"
_CBR = "https://www.cbr.ru/scripts/XML_daily.asp"
_QUOTES = ("USDT", "USDC", "FDUSD")
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_COIN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,11}$")
_MAX_JSON_BYTES = 20 * 1024 * 1024
_MAX_XML_BYTES = 1_000_000
_TIMEFRAMES: dict[str, tuple[str, str, int]] = {
    "1h": ("1m", "1min", 60),
    "24h": ("5m", "5min", 288),
    "7d": ("1h", "1hour", 168),
}

_KNOWN_COINS = (
    Coin("bitcoin", "BTC", "Bitcoin", 1),
    Coin("ethereum", "ETH", "Ethereum", 2),
    Coin("tether", "USDT", "Tether", 3),
    Coin("binancecoin", "BNB", "BNB", 4),
    Coin("solana", "SOL", "Solana", 5),
    Coin("usd-coin", "USDC", "USDC", 6),
    Coin("ripple", "XRP", "XRP", 7),
    Coin("dogecoin", "DOGE", "Dogecoin", 8),
    Coin("cardano", "ADA", "Cardano", 10),
    Coin("tron", "TRX", "TRON", 11),
    Coin("the-open-network", "TON", "Toncoin", 15),
    Coin("chainlink", "LINK", "Chainlink", 16),
    Coin("avalanche-2", "AVAX", "Avalanche", 18),
    Coin("polkadot", "DOT", "Polkadot", 20),
    Coin("litecoin", "LTC", "Litecoin", 21),
    Coin("monero", "XMR", "Monero", 30),
)
_KNOWN_BY_QUERY = {
    key: coin
    for coin in _KNOWN_COINS
    for key in (coin.id.casefold(), coin.symbol.casefold(), coin.name.casefold())
}
_EXCHANGE_SYMBOL_BY_COIN_ID = {coin.id: coin.symbol for coin in _KNOWN_COINS}


class MarketUnavailable(RuntimeError):
    """Raised when no market provider can fulfill a request."""


class MarketService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._search_cache: TTLCache[Coin | None] = TTLCache(2_000)
        self._quote_cache: TTLCache[Quote] = TTLCache(2_000)
        self._chart_cache: TTLCache[Chart] = TTLCache(500)
        self._pairs_cache: TTLCache[frozenset[str]] = TTLCache(4)
        self._cbr_cache: TTLCache[tuple[Decimal, str]] = TTLCache(2)
        self._pairs_locks = {"binance": asyncio.Lock(), "kucoin": asyncio.Lock()}
        self._search_semaphore = asyncio.Semaphore(5)
        self._quote_semaphore = asyncio.Semaphore(settings.query_concurrency)

    async def start(self) -> None:
        if self._client is not None:
            return
        timeout = httpx.Timeout(
            self._settings.http_timeout,
            connect=min(self._settings.http_timeout, 8.0),
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=20.0,
            ),
            headers={
                "User-Agent": "CryptoMathXBot/2.0 (+https://github.com/Omnividente/CryptoMathXBot)"
            },
            follow_redirects=False,
            http2=True,
        )

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def resolve_coin(self, query: str) -> Coin | None:
        normalized = query.strip().lstrip("$").casefold()
        if not normalized or len(normalized) > 64:
            return None
        known = _KNOWN_BY_QUERY.get(normalized)
        if known is not None:
            return known
        cached = self._search_cache.get(normalized)
        if cached is not None:
            return cached.value

        async with self._search_semaphore:
            cached = self._search_cache.get(normalized)
            if cached is not None:
                return cached.value
            try:
                payload = await self._request_json(
                    "GET", f"{_COINGECKO}/search", params={"query": normalized}
                )
                raw_coins = payload.get("coins", []) if isinstance(payload, dict) else []
                matches: list[Coin] = []
                for item in raw_coins:
                    if not isinstance(item, dict):
                        continue
                    coin_id = str(item.get("id") or "")
                    if _COIN_ID_RE.fullmatch(coin_id) is None:
                        continue
                    symbol = str(item.get("symbol") or "").upper()[:32]
                    raw_name = str(item.get("name") or "")[:256]
                    if _SYMBOL_RE.fullmatch(symbol) is None:
                        continue
                    if normalized not in {
                        coin_id.casefold(),
                        symbol.casefold(),
                        raw_name.casefold(),
                    }:
                        continue
                    name = (raw_name.strip() or symbol)[:80]
                    rank_value = item.get("market_cap_rank")
                    rank = int(rank_value) if isinstance(rank_value, int) else None
                    matches.append(Coin(coin_id, symbol, name, rank))
                matches.sort(key=lambda coin: coin.market_cap_rank or 1_000_000)
                result = matches[0] if matches else None
            except (ValueError, TypeError):
                result = None
            self._search_cache.set(
                normalized,
                result,
                ttl=24 * 3600 if result is not None else 15 * 60,
            )
            return result

    async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, Coin]:
        unique = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        values = await asyncio.gather(*(self.resolve_coin(symbol) for symbol in unique))
        return {
            symbol: coin for symbol, coin in zip(unique, values, strict=True) if coin is not None
        }

    async def quotes(
        self,
        coins: tuple[Coin, ...],
        *,
        force_refresh: bool = False,
    ) -> dict[str, Quote]:
        unique = {coin.id: coin for coin in coins}
        result: dict[str, Quote] = {}
        missing: list[Coin] = []
        for coin in unique.values():
            cached = None if force_refresh else self._quote_cache.get(coin.id)
            if cached is None:
                missing.append(coin)
            else:
                result[coin.symbol] = cached.value
        if not missing:
            return result

        pair_sets = await asyncio.gather(
            self._pairs("binance"),
            self._pairs("kucoin"),
            return_exceptions=True,
        )
        binance_pairs = pair_sets[0] if isinstance(pair_sets[0], frozenset) else frozenset()
        kucoin_pairs = pair_sets[1] if isinstance(pair_sets[1], frozenset) else frozenset()

        exchange_values = await asyncio.gather(
            *(self._exchange_quote(coin, binance_pairs, kucoin_pairs) for coin in missing),
            return_exceptions=True,
        )
        still_missing: list[Coin] = []
        for coin, value in zip(missing, exchange_values, strict=True):
            if isinstance(value, Quote):
                result[coin.symbol] = value
                self._quote_cache.set(coin.id, value, ttl=20, stale_ttl=5 * 60)
            else:
                still_missing.append(coin)

        if still_missing:
            coingecko = await self._coingecko_quotes(still_missing)
            for coin in tuple(still_missing):
                value = coingecko.get(coin.id)
                if value is None:
                    continue
                result[coin.symbol] = value
                self._quote_cache.set(coin.id, value, ttl=30, stale_ttl=5 * 60)
                still_missing.remove(coin)

        if still_missing:
            paprika_values = await asyncio.gather(
                *(self._coinpaprika_quote(coin) for coin in still_missing),
                return_exceptions=True,
            )
            for coin, value in zip(still_missing, paprika_values, strict=True):
                if isinstance(value, Quote):
                    result[coin.symbol] = value
                    self._quote_cache.set(coin.id, value, ttl=30, stale_ttl=5 * 60)

        for coin in unique.values():
            if coin.symbol in result:
                continue
            stale = self._quote_cache.get(coin.id, allow_stale=True)
            if stale is not None:
                result[coin.symbol] = replace(stale.value, stale=True)
        return result

    async def usd_rub(self) -> tuple[Decimal | None, str | None]:
        cached = self._cbr_cache.get("usd")
        if cached is not None:
            return cached.value
        try:
            response = await self._request("GET", _CBR, max_bytes=_MAX_XML_BYTES)
            root = SafeET.fromstring(response.content, forbid_dtd=True)
            value: Decimal | None = None
            for currency in root.findall("Valute"):
                if (currency.findtext("CharCode") or "") != "USD":
                    continue
                nominal = Decimal(currency.findtext("Nominal") or "1")
                raw = (currency.findtext("Value") or "").replace(",", ".")
                value = Decimal(raw) / nominal
                break
            if value is None or value <= 0:
                raise MarketUnavailable("USD rate is absent")
            cbr_date = root.attrib.get("Date") or datetime.now().strftime("%d.%m.%Y")
            self._cbr_cache.set("usd", (value, cbr_date), ttl=3600, stale_ttl=3 * 86400)
            return value, cbr_date
        except (
            MarketUnavailable,
            StdET.ParseError,
            DefusedXmlException,
            InvalidOperation,
            ZeroDivisionError,
        ):
            stale = self._cbr_cache.get("usd", allow_stale=True)
            return stale.value if stale is not None else (None, None)

    async def chart(
        self,
        quote: Quote,
        timeframe: str,
        *,
        force_refresh: bool = False,
    ) -> Chart:
        if timeframe not in _TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        key = f"{quote.coin.id}:{quote.source}:{quote.pair}:{timeframe}"
        if not force_refresh:
            cached = self._chart_cache.get(key)
            if cached is not None:
                return cached.value

        chart: Chart | None = None
        try:
            if quote.source == "Binance" and quote.pair:
                chart = await self._binance_chart(quote.coin.symbol, quote.pair, timeframe)
            elif quote.source == "KuCoin" and quote.pair:
                chart = await self._kucoin_chart(quote.coin.symbol, quote.pair, timeframe)
        except MarketUnavailable:
            chart = None
        if chart is None:
            try:
                chart = await self._coingecko_chart(quote.coin, timeframe)
            except MarketUnavailable:
                stale = self._chart_cache.get(key, allow_stale=True)
                if stale is not None:
                    return stale.value
                raise
        self._chart_cache.set(key, chart, ttl=60, stale_ttl=5 * 60)
        return chart

    async def _pairs(self, provider: str) -> frozenset[str]:
        cached = self._pairs_cache.get(provider)
        if cached is not None:
            return cached.value
        lock = self._pairs_locks[provider]
        async with lock:
            cached = self._pairs_cache.get(provider)
            if cached is not None:
                return cached.value
            try:
                if provider == "binance":
                    payload = await self._request_json("GET", f"{_BINANCE}/api/v3/exchangeInfo")
                    items = payload.get("symbols", []) if isinstance(payload, dict) else []
                    pairs = frozenset(
                        str(item.get("symbol") or "").upper()
                        for item in items
                        if isinstance(item, dict)
                        and str(item.get("status") or "").upper() == "TRADING"
                    )
                else:
                    payload = await self._request_json("GET", f"{_KUCOIN}/api/v2/symbols")
                    items = payload.get("data", []) if isinstance(payload, dict) else []
                    pairs = frozenset(
                        str(item.get("symbol") or "").upper()
                        for item in items
                        if isinstance(item, dict)
                        and item.get("enableTrading") is not False
                        and str(item.get("quoteCurrency") or "").upper() in _QUOTES
                    )
                if not pairs:
                    raise MarketUnavailable(f"{provider} returned no pairs")
                self._pairs_cache.set(provider, pairs, ttl=4 * 3600, stale_ttl=24 * 3600)
                return pairs
            except MarketUnavailable:
                stale = self._pairs_cache.get(provider, allow_stale=True)
                if stale is not None:
                    return stale.value
                raise

    async def _exchange_quote(
        self,
        coin: Coin,
        binance_pairs: frozenset[str],
        kucoin_pairs: frozenset[str],
    ) -> Quote | None:
        exchange_symbol = _EXCHANGE_SYMBOL_BY_COIN_ID.get(coin.id)
        if exchange_symbol is None:
            return None
        async with self._quote_semaphore:
            for quote_symbol in _QUOTES:
                pair = f"{exchange_symbol}{quote_symbol}"
                if pair not in binance_pairs:
                    continue
                try:
                    payload = await self._request_json(
                        "GET",
                        f"{_BINANCE}/api/v3/ticker/24hr",
                        params={"symbol": pair},
                    )
                    price = _positive_decimal(payload.get("lastPrice"))
                    change = _decimal_or_none(payload.get("priceChangePercent"))
                    if price is not None:
                        return Quote(
                            coin,
                            price,
                            change,
                            "Binance",
                            pair,
                            datetime.now(timezone.utc),
                        )
                except MarketUnavailable:
                    break

            for quote_symbol in _QUOTES:
                pair = f"{exchange_symbol}-{quote_symbol}"
                if pair not in kucoin_pairs:
                    continue
                try:
                    payload = await self._request_json(
                        "GET",
                        f"{_KUCOIN}/api/v1/market/stats",
                        params={"symbol": pair},
                    )
                    data = payload.get("data", {}) if isinstance(payload, dict) else {}
                    price = _positive_decimal(data.get("last"))
                    raw_change = _decimal_or_none(data.get("changeRate"))
                    change = raw_change * 100 if raw_change is not None else None
                    if price is not None:
                        return Quote(
                            coin,
                            price,
                            change,
                            "KuCoin",
                            pair,
                            datetime.now(timezone.utc),
                        )
                except MarketUnavailable:
                    break
        return None

    async def _coingecko_quotes(self, coins: list[Coin]) -> dict[str, Quote]:
        if not coins:
            return {}
        try:
            payload = await self._request_json(
                "GET",
                f"{_COINGECKO}/simple/price",
                params={
                    "ids": ",".join(coin.id for coin in coins),
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
            )
        except MarketUnavailable:
            return {}
        result: dict[str, Quote] = {}
        for coin in coins:
            item = payload.get(coin.id, {}) if isinstance(payload, dict) else {}
            price = _positive_decimal(item.get("usd")) if isinstance(item, dict) else None
            if price is None:
                continue
            change = _decimal_or_none(item.get("usd_24h_change"))
            result[coin.id] = Quote(
                coin,
                price,
                change,
                "CoinGecko",
                None,
                datetime.now(timezone.utc),
            )
        return result

    async def _coinpaprika_quote(self, coin: Coin) -> Quote | None:
        try:
            search = await self._request_json(
                "GET",
                f"{_COINPAPRIKA}/search",
                params={"q": coin.symbol, "c": "currencies", "limit": 10},
            )
            items = search.get("currencies", []) if isinstance(search, dict) else []
            match = next(
                (
                    item
                    for item in items
                    if isinstance(item, dict)
                    and str(item.get("symbol") or "").upper() == coin.symbol
                    and str(item.get("name") or "")[:80].casefold() == coin.name.casefold()
                ),
                None,
            )
            paprika_id = str(match.get("id") or "") if match else ""
            if _COIN_ID_RE.fullmatch(paprika_id) is None:
                return None
            payload = await self._request_json("GET", f"{_COINPAPRIKA}/tickers/{paprika_id}")
            usd = payload.get("quotes", {}).get("USD", {}) if isinstance(payload, dict) else {}
            price = _positive_decimal(usd.get("price")) if isinstance(usd, dict) else None
            if price is None:
                return None
            return Quote(
                coin,
                price,
                _decimal_or_none(usd.get("percent_change_24h")),
                "CoinPaprika",
                None,
                datetime.now(timezone.utc),
            )
        except MarketUnavailable:
            return None

    async def _binance_chart(self, symbol: str, pair: str, timeframe: str) -> Chart:
        interval, _, limit = _TIMEFRAMES[timeframe]
        payload = await self._request_json(
            "GET",
            f"{_BINANCE}/api/v3/klines",
            params={"symbol": pair, "interval": interval, "limit": limit},
        )
        points = tuple(
            (int(row[0]), float(row[4]))
            for row in payload
            if isinstance(row, list) and len(row) >= 5
        )
        if len(points) < 2:
            raise MarketUnavailable("Binance chart is empty")
        return Chart(symbol, timeframe, points, "Binance")

    async def _kucoin_chart(self, symbol: str, pair: str, timeframe: str) -> Chart:
        _, interval, _ = _TIMEFRAMES[timeframe]
        seconds = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}[timeframe]
        end_at = int(time.time())
        payload = await self._request_json(
            "GET",
            f"{_KUCOIN}/api/v1/market/candles",
            params={
                "symbol": pair,
                "type": interval,
                "startAt": end_at - seconds,
                "endAt": end_at,
            },
        )
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        points = sorted(
            (
                (int(float(row[0])) * 1000, float(row[2]))
                for row in rows
                if isinstance(row, list) and len(row) >= 3
            ),
            key=lambda point: point[0],
        )
        if len(points) < 2:
            raise MarketUnavailable("KuCoin chart is empty")
        return Chart(symbol, timeframe, tuple(points), "KuCoin")

    async def _coingecko_chart(self, coin: Coin, timeframe: str) -> Chart:
        days = "7" if timeframe == "7d" else "1"
        payload = await self._request_json(
            "GET",
            f"{_COINGECKO}/coins/{coin.id}/market_chart",
            params={"vs_currency": "usd", "days": days},
        )
        rows = payload.get("prices", []) if isinstance(payload, dict) else []
        points = tuple(
            (int(row[0]), float(row[1])) for row in rows if isinstance(row, list) and len(row) >= 2
        )
        if timeframe == "1h" and points:
            cutoff = points[-1][0] - 3600 * 1000
            points = tuple(point for point in points if point[0] >= cutoff)
        if len(points) < 2:
            raise MarketUnavailable("CoinGecko chart is empty")
        return Chart(coin.symbol, timeframe, points, "CoinGecko")

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self._request(method, url, max_bytes=_MAX_JSON_BYTES, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise MarketUnavailable("provider returned invalid JSON") from exc

    async def _request(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        **kwargs: Any,
    ) -> httpx.Response:
        client = self._client
        if client is None:
            raise RuntimeError("MarketService.start() was not called")
        last_error: Exception | None = None
        for attempt in range(self._settings.http_retries + 1):
            try:
                async with client.stream(method, url, **kwargs) as response:
                    if response.status_code not in _RETRY_STATUSES:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise MarketUnavailable(
                                f"provider HTTP {response.status_code}"
                            ) from exc
                        if _content_length(response) > max_bytes:
                            raise MarketUnavailable("provider response is unexpectedly large")
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise MarketUnavailable("provider response is unexpectedly large")
                            chunks.append(chunk)
                        decoded_headers = [
                            (name, value)
                            for name, value in response.headers.multi_items()
                            if name.casefold() not in {"content-encoding", "content-length"}
                        ]
                        return httpx.Response(
                            status_code=response.status_code,
                            headers=decoded_headers,
                            content=b"".join(chunks),
                            request=response.request,
                        )
                    last_error = MarketUnavailable(f"provider HTTP {response.status_code}")
                    if attempt >= self._settings.http_retries:
                        break
                    retry_after = _retry_after(response)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= self._settings.http_retries:
                    break
                retry_after = 0.0
            delay = max(retry_after, min(4.0, 0.4 * (2**attempt) + random.random() * 0.2))
            await asyncio.sleep(delay)
        _LOGGER.warning(
            "market request failed provider=%s error=%s",
            _provider_name(url),
            type(last_error).__name__,
        )
        raise MarketUnavailable("market provider is temporarily unavailable") from last_error


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return min(10.0, max(0.0, float(raw)))
    except ValueError:
        return 0.0


def _content_length(response: httpx.Response) -> int:
    raw = response.headers.get("Content-Length", "")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _provider_name(url: str) -> str:
    match = re.match(r"https://([^/]+)", url)
    return cast(str, match.group(1) if match else "unknown")
