import gzip
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from cryptomathxbot.config import Settings
from cryptomathxbot.domain import Chart, Coin, Quote
from cryptomathxbot.market import MarketService, MarketUnavailable


def settings(*, retries: int = 1) -> Settings:
    return Settings(
        token="test-token",
        data_dir=Path("data"),
        log_dir=Path("logs"),
        legacy_favorites_file=Path("favorites.json"),
        owner_chat_id=None,
        log_level="INFO",
        default_favorites=("BTC", "ETH", "XMR"),
        max_favorites=8,
        max_symbols=8,
        concurrent_updates=4,
        query_concurrency=4,
        rate_limit_requests=8,
        rate_limit_window=30,
        http_timeout=2,
        http_retries=retries,
        chart_dpi=100,
    )


@pytest.mark.asyncio
async def test_exchange_quotes_cbr_and_chart_are_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/v3/exchangeInfo"):
            return httpx.Response(
                200,
                json={"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
            )
        if path.endswith("/api/v2/symbols"):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/api/v3/ticker/24hr"):
            return httpx.Response(
                200,
                json={"lastPrice": "123.45", "priceChangePercent": "2.5"},
            )
        if path.endswith("/api/v3/klines"):
            return httpx.Response(
                200,
                json=[[1_000, "0", "0", "0", "100"], [2_000, "0", "0", "0", "110"]],
            )
        if path.endswith("/XML_daily.asp"):
            return httpx.Response(
                200,
                content=(
                    b'<ValCurs Date="02.09.2026"><Valute><CharCode>USD</CharCode>'
                    b"<Nominal>1</Nominal><Value>80,50</Value></Valute></ValCurs>"
                ),
            )
        raise AssertionError(str(request.url))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(settings(), client)
        coin = await service.resolve_coin("BTC")
        assert coin is not None
        quotes = await service.quotes((coin,))
        rate, rate_date = await service.usd_rub()
        chart = await service.chart(quotes["BTC"], "24h")

    assert str(quotes["BTC"].usd) == "123.45"
    assert str(quotes["BTC"].change_24h) == "2.5"
    assert quotes["BTC"].source == "Binance"
    assert str(rate) == "80.50"
    assert rate_date == "02.09.2026"
    assert chart.points[-1] == (2_000, 110.0)


@pytest.mark.asyncio
async def test_coingecko_is_used_when_exchanges_have_no_pair() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/v3/exchangeInfo"):
            return httpx.Response(200, json={"symbols": []})
        if path.endswith("/api/v2/symbols"):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/simple/price"):
            return httpx.Response(
                200,
                json={"monero": {"usd": 500.25, "usd_24h_change": -1.25}},
            )
        raise AssertionError(str(request.url))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(settings(retries=0), client)
        coin = await service.resolve_coin("XMR")
        assert coin is not None
        quotes = await service.quotes((coin,))

    assert quotes["XMR"].source == "CoinGecko"
    assert str(quotes["XMR"].usd) == "500.25"


@pytest.mark.asyncio
async def test_retry_after_is_honored_for_transient_search_failure(monkeypatch: Any) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(
            200,
            json={
                "coins": [
                    {
                        "id": "example-coin",
                        "symbol": "EXM",
                        "name": "Example Coin",
                        "market_cap_rank": 999,
                    }
                ]
            },
        )

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("cryptomathxbot.market.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(settings(retries=1), client)
        coin = await service.resolve_coin("EXM")

    assert coin is not None and coin.id == "example-coin"
    assert requests == 2


@pytest.mark.asyncio
async def test_dynamic_coin_never_uses_an_exchange_pair_by_symbol_alone() -> None:
    ticker_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ticker_requests
        if request.url.path.endswith("/api/v3/exchangeInfo"):
            return httpx.Response(
                200,
                json={"symbols": [{"symbol": "XAIUSDT", "status": "TRADING"}]},
            )
        if request.url.path.endswith("/api/v2/symbols"):
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/api/v3/ticker/24hr"):
            ticker_requests += 1
            return httpx.Response(200, json={"lastPrice": "0.00731"})
        if request.url.path.endswith("/simple/price"):
            return httpx.Response(
                200,
                json={"sideshift-token": {"usd": 0.068015, "usd_24h_change": 1.0}},
            )
        raise AssertionError(str(request.url))

    coin = Coin("sideshift-token", "XAI", "SideShift")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        quotes = await MarketService(settings(retries=0), client).quotes((coin,))

    assert ticker_requests == 0
    assert quotes["XAI"].source == "CoinGecko"
    assert quotes["XAI"].usd == Decimal("0.068015")


@pytest.mark.asyncio
async def test_request_retries_all_httpx_transport_errors(monkeypatch: Any) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.RemoteProtocolError("peer disconnected", request=request)
        return httpx.Response(
            200,
            json={
                "coins": [
                    {
                        "id": "example-coin",
                        "symbol": "EXM",
                        "name": "Example Coin",
                        "market_cap_rank": 999,
                    }
                ]
            },
        )

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("cryptomathxbot.market.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        coin = await MarketService(settings(retries=1), client).resolve_coin("EXM")

    assert coin is not None and coin.id == "example-coin"
    assert requests == 2


@pytest.mark.asyncio
async def test_streamed_compressed_json_is_decoded_exactly_once() -> None:
    payload = gzip.compress(
        b'{"coins":[{"id":"example-coin","symbol":"EXM","name":"Example Coin"}]}'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Encoding": "gzip", "Content-Length": str(len(payload))},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        coin = await MarketService(settings(retries=0), client).resolve_coin("EXM")

    assert coin is not None
    assert coin.id == "example-coin"


@pytest.mark.asyncio
async def test_permanent_http_status_is_not_retried() -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(settings(retries=2), client)
        with pytest.raises(MarketUnavailable):
            await service.resolve_coin("EXM")

    assert requests == 1


@pytest.mark.asyncio
async def test_search_transport_failure_is_not_negative_cached() -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503, json={})
        return httpx.Response(
            200,
            json={
                "coins": [
                    {
                        "id": "example-coin",
                        "symbol": "EXM",
                        "name": "Example Coin",
                        "market_cap_rank": 999,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(settings(retries=0), client)
        with pytest.raises(MarketUnavailable):
            await service.resolve_coin("EXM")
        coin = await service.resolve_coin("EXM")

    assert coin is not None and coin.id == "example-coin"
    assert requests == 2


@pytest.mark.asyncio
async def test_cbr_rejects_dtd_and_tolerates_transport_failure() -> None:
    responses: list[bytes | None] = [
        b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///BOT_TOKEN.txt">]><x>&secret;</x>',
        None,
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        value = responses.pop(0)
        if value is None:
            raise httpx.RemoteProtocolError("peer disconnected", request=request)
        return httpx.Response(200, content=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(settings(retries=0), client)
        assert await service.usd_rub() == (None, None)
        assert await service.usd_rub() == (None, None)


@pytest.mark.asyncio
async def test_malformed_provider_coin_id_is_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "coins": [
                    {
                        "id": "../../../internal",
                        "symbol": "EXM",
                        "name": "Example Coin",
                        "market_cap_rank": 1,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        coin = await MarketService(settings(retries=0), client).resolve_coin("EXM")

    assert coin is None


@pytest.mark.asyncio
async def test_chart_uses_stale_cache_when_all_providers_fail(monkeypatch: Any) -> None:
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    quote = Quote(
        coin,
        Decimal("100"),
        None,
        "CoinGecko",
        None,
        datetime.now(timezone.utc),
    )
    previous = Chart("BTC", "24h", ((1_000, 99.0), (2_000, 100.0)), "CoinGecko")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(settings(retries=0), client)
        key = "bitcoin:CoinGecko:None:24h"
        service._chart_cache.set(key, previous, ttl=60, stale_ttl=300, now=100)
        monkeypatch.setattr("cryptomathxbot.cache.time.monotonic", lambda: 161)

        chart = await service.chart(quote, "24h")

    assert chart is previous


@pytest.mark.asyncio
async def test_provider_coin_labels_are_bounded_for_telegram_output() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "coins": [
                    {
                        "id": "example-coin",
                        "symbol": "EXM",
                        "name": "X" * 1_000,
                        "market_cap_rank": 1,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        coin = await MarketService(settings(retries=0), client).resolve_coin("EXM")

    assert coin is not None
    assert len(coin.name) == 80


@pytest.mark.asyncio
async def test_oversized_json_response_is_rejected(monkeypatch: Any) -> None:
    monkeypatch.setattr("cryptomathxbot.market._MAX_JSON_BYTES", 4)

    chunks_read: list[bytes] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"12", b"345", b"never-read"):
                chunks_read.append(chunk)
                yield chunk

        async def aclose(self) -> None:
            return None

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MarketService(settings(retries=0), client)
        with pytest.raises(MarketUnavailable, match="unexpectedly large"):
            await service.resolve_coin("EXM")
    assert chunks_read == [b"12", b"345"]
