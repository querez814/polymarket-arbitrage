import asyncio
from datetime import timezone

import httpx
import pytest

from kalshi_client.api import KalshiClient
from kalshi_client.models import KalshiMarket
from polymarket_client.api import OrderBookNormalizationError, PolymarketClient
from polymarket_client.models import Market, TokenType


class _ControlledCloseClient:
    def __init__(self, *outcomes: str):
        self.outcomes = list(outcomes)
        self.close_calls = 0

    async def aclose(self):
        self.close_calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "success"
        if outcome == "timeout":
            await asyncio.Event().wait()
        if outcome == "error":
            raise RuntimeError("close failed")


def test_polymarket_suppresses_token_after_first_no_orderbook_response():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request, json={"error": "not found"})

    async def exercise():
        client = PolymarketClient(max_retries=1)
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = await client._fetch_token_orderbook("stale-token", TokenType.YES)
            second = await client._fetch_token_orderbook("stale-token", TokenType.YES)
            return first, second, client.orderbook_metrics
        finally:
            await client._http_client.aclose()
            client._http_client = None

    first, second, metrics = asyncio.run(exercise())

    assert calls == 1
    assert first.bids.levels == second.bids.levels == []
    assert metrics == {
        "requests": 1,
        "successes": 0,
        "not_found": 1,
        "normalization_failures": 0,
    }


def test_polymarket_does_not_turn_upstream_503_into_an_empty_book():
    def handler(request: httpx.Request):
        return httpx.Response(503, request=request, json={"error": "unavailable"})

    async def exercise():
        client = PolymarketClient(max_retries=1)
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client._fetch_token_orderbook("live-token", TokenType.YES)
        finally:
            await client._http_client.aclose()
            client._http_client = None

    asyncio.run(exercise())


def test_polymarket_orderbook_normalizes_raw_venue_depth_before_selecting_best():
    """The CLOB API returns outer levels first; public books expose executable best."""

    def handler(request: httpx.Request):
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "559662",
                    "conditionId": "condition-559662",
                    "question": "Will Mark Cuban win?",
                    "clobTokenIds": '["yes-token", "no-token"]',
                    "active": True,
                    "closed": False,
                },
            )
        token_id = request.url.params["token_id"]
        if token_id == "yes-token":
            payload = {
                "bids": [
                    {"price": "0.001", "size": "2000"},
                    {"price": "0.004", "size": "40"},
                    {"price": "0.005", "size": "56"},
                ],
                "asks": [
                    {"price": "0.999", "size": "5000"},
                    {"price": "0.010", "size": "100"},
                    {"price": "0.006", "size": "41"},
                ],
            }
        else:
            payload = {
                "bids": [
                    {"price": "0.001", "size": "5000"},
                    {"price": "0.993", "size": "41"},
                    {"price": "0.994", "size": "100"},
                ],
                "asks": [
                    {"price": "0.999", "size": "2000"},
                    {"price": "0.997", "size": "40"},
                    {"price": "0.996", "size": "56"},
                ],
            }
        return httpx.Response(200, request=request, json=payload)

    async def exercise():
        client = PolymarketClient(max_retries=1, dry_run=False)
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get_orderbook("559662")
        finally:
            await client._http_client.aclose()
            client._http_client = None

    book = asyncio.run(exercise())

    assert book.best_bid_yes == 0.005
    assert book.yes.best_bid_size == 56.0
    assert book.best_ask_yes == 0.006
    assert book.yes.best_ask_size == 41.0
    assert book.best_bid_no == 0.994
    assert book.best_ask_no == 0.996


def test_polymarket_orderbook_sorts_before_applying_depth_limit():
    outer_bids = [
        {"price": f"{value / 1000:.3f}", "size": "10"} for value in range(1, 12)
    ]
    outer_asks = [
        {"price": f"{value / 1000:.3f}", "size": "10"} for value in range(999, 988, -1)
    ]

    def handler(request: httpx.Request):
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "depth-1",
                    "conditionId": "condition-depth-1",
                    "question": "Depth normalization?",
                    "clobTokenIds": '["yes-depth", "no-depth"]',
                    "active": True,
                    "closed": False,
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={"bids": outer_bids, "asks": outer_asks},
        )

    async def exercise():
        client = PolymarketClient(max_retries=1, dry_run=False)
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get_orderbook("depth-1")
        finally:
            await client._http_client.aclose()
            client._http_client = None

    book = asyncio.run(exercise())

    assert book.best_bid_yes == 0.011
    assert book.best_ask_yes == 0.989
    assert len(book.yes.bids.levels) == 10
    assert len(book.yes.asks.levels) == 10


def test_polymarket_crossed_or_invalid_raw_book_fails_closed_with_reason_code():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            request=request,
            json={
                "bids": [{"price": "0.60", "size": "10"}],
                "asks": [{"price": "0.50", "size": "10"}],
            },
        )

    async def exercise():
        client = PolymarketClient(max_retries=1)
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(OrderBookNormalizationError) as captured:
                await client._fetch_token_orderbook("crossed-token", TokenType.YES)
            return captured.value, client.orderbook_metrics
        finally:
            await client._http_client.aclose()
            client._http_client = None

    error, metrics = asyncio.run(exercise())

    assert error.reason_code == "polymarket_orderbook_normalization_failed"
    assert error.evidence["token_id"] == "crossed-token"
    assert metrics["normalization_failures"] == 1


def test_polymarket_active_market_list_uses_ttl_cache():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "id": "1",
                    "conditionId": "condition-1",
                    "question": "Will Alice win?",
                    "clobTokenIds": '["yes-1", "no-1"]',
                    "active": True,
                    "closed": False,
                    "createdAt": "2026-07-01T12:30:00Z",
                    "endDate": "2026-08-01T00:00:00+00:00",
                }
            ],
        )

    async def exercise():
        client = PolymarketClient(max_retries=1)
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = await client.list_markets({"active": True})
            second = await client.list_markets({"active": True})
            return first, second, client.request_metrics
        finally:
            await client._http_client.aclose()
            client._http_client = None

    first, second, metrics = asyncio.run(exercise())

    assert len(first) == len(second) == 1
    assert calls == 1
    assert first[0].created_at is not None
    assert first[0].created_at.tzinfo == timezone.utc
    assert first[0].end_date is not None
    assert first[0].end_date.month == 8
    assert metrics["https://gamma-api.polymarket.com"]["requests"] == 1


def test_polymarket_orderbook_uses_cached_market_metadata():
    gamma_calls = 0
    clob_calls = 0

    def handler(request: httpx.Request):
        nonlocal gamma_calls, clob_calls
        if request.url.host == "gamma-api.polymarket.com":
            gamma_calls += 1
            pytest.fail("cached orderbook lookup must not call Gamma")
        clob_calls += 1
        return httpx.Response(
            200,
            request=request,
            json={
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.60", "size": "10"}],
            },
        )

    async def exercise():
        client = PolymarketClient(max_retries=1)
        client._cache_market(
            Market(
                market_id="cached-1",
                condition_id="condition-cached-1",
                question="Cached market?",
                yes_token_id="yes-cached-1",
                no_token_id="no-cached-1",
                active=True,
            )
        )
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get_orderbook("cached-1")
        finally:
            await client._http_client.aclose()
            client._http_client = None

    book = asyncio.run(exercise())

    assert gamma_calls == 0
    assert clob_calls == 2
    assert book.yes.bids.best_price == pytest.approx(0.40)
    assert book.no.asks.best_price == pytest.approx(0.60)


def test_polymarket_http_pool_recycle_is_atomic_and_rate_limited():
    async def exercise():
        client = PolymarketClient()
        await client.connect()
        try:
            first = await client.recycle_http_client(reason="snapshot_starvation")
            second = await client.recycle_http_client(reason="snapshot_starvation")
            return first, second, client.connection_metrics
        finally:
            await client.disconnect()

    first, second, metrics = asyncio.run(exercise())

    assert first is True
    assert second is False
    assert metrics == {
        "pool_recycles": 1,
        "recycle_suppressed": 1,
        "retired_pools": 0,
        "retired_pool_close_failures": 0,
        "last_recycle_reason": "snapshot_starvation",
    }


def test_kalshi_http_pool_recycle_is_atomic_and_rate_limited():
    async def exercise():
        async with KalshiClient() as client:
            first = await client.recycle_http_client(reason="snapshot_starvation")
            second = await client.recycle_http_client(reason="snapshot_starvation")
            return first, second, client.connection_metrics

    first, second, metrics = asyncio.run(exercise())

    assert first is True
    assert second is False
    assert metrics == {
        "pool_recycles": 1,
        "recycle_suppressed": 1,
        "retired_pools": 0,
        "retired_pool_close_failures": 0,
        "last_recycle_reason": "snapshot_starvation",
    }


def test_polymarket_failed_pool_close_is_bounded_and_retried_on_shutdown():
    async def exercise():
        client = PolymarketClient()
        exhausted = _ControlledCloseClient("timeout", "timeout", "success")
        replacement = _ControlledCloseClient("success")
        builds = 0

        def build_client():
            nonlocal builds
            builds += 1
            return replacement

        client._http_client = exhausted
        client._build_http_client = build_client
        client._http_close_timeout_seconds = 0.001

        first = await client.recycle_http_client(reason="snapshot_starvation")
        client._last_http_recycle_at = float("-inf")
        second = await client.recycle_http_client(reason="snapshot_starvation")
        before_shutdown = client.connection_metrics
        await client.disconnect()
        return (
            first,
            second,
            builds,
            exhausted.close_calls,
            before_shutdown,
            client.connection_metrics,
        )

    first, second, builds, close_calls, before_shutdown, after_shutdown = asyncio.run(
        exercise()
    )

    assert first is True
    assert second is False
    assert builds == 1
    assert close_calls == 3
    assert before_shutdown["retired_pools"] == 1
    assert before_shutdown["retired_pool_close_failures"] == 2
    assert after_shutdown["retired_pools"] == 0


def test_kalshi_failed_pool_close_is_bounded_and_retried_on_shutdown():
    async def exercise():
        client = KalshiClient()
        exhausted = _ControlledCloseClient("error", "error", "success")
        replacement = _ControlledCloseClient("success")
        builds = 0

        def build_client():
            nonlocal builds
            builds += 1
            return replacement

        client._client = exhausted
        client._build_http_client = build_client
        client._http_close_timeout_seconds = 0.001

        first = await client.recycle_http_client(reason="snapshot_starvation")
        client._last_http_recycle_at = float("-inf")
        second = await client.recycle_http_client(reason="snapshot_starvation")
        before_shutdown = client.connection_metrics
        await client.__aexit__(None, None, None)
        return (
            first,
            second,
            builds,
            exhausted.close_calls,
            before_shutdown,
            client.connection_metrics,
        )

    first, second, builds, close_calls, before_shutdown, after_shutdown = asyncio.run(
        exercise()
    )

    assert first is True
    assert second is False
    assert builds == 1
    assert close_calls == 3
    assert before_shutdown["retired_pools"] == 1
    assert before_shutdown["retired_pool_close_failures"] == 2
    assert after_shutdown["retired_pools"] == 0


def test_polymarket_market_pagination_does_not_use_rejected_volume_sort():
    observed_queries = []

    def handler(request: httpx.Request):
        observed_queries.append(dict(request.url.params))
        return httpx.Response(200, request=request, json=[])

    async def exercise():
        client = PolymarketClient(max_retries=1)
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.list_markets({"active": True})
        finally:
            await client._http_client.aclose()
            client._http_client = None

    asyncio.run(exercise())

    assert observed_queries
    assert "order" not in observed_queries[0]
    assert "ascending" not in observed_queries[0]


def test_kalshi_event_market_list_uses_ttl_cache():
    async def exercise():
        client = KalshiClient()
        calls = 0

        async def page(**_kwargs):
            nonlocal calls
            calls += 1
            return [
                KalshiMarket(
                    ticker="KX-1",
                    event_ticker="KXE-1",
                    series_ticker="KXS",
                    title="Alice wins?",
                )
            ], None

        client.list_event_markets = page
        first = await client.list_all_event_markets(max_events=100, max_markets=100)
        second = await client.list_all_event_markets(max_events=100, max_markets=100)
        return calls, first, second

    calls, first, second = asyncio.run(exercise())

    assert calls == 1
    assert first[0].ticker == second[0].ticker == "KX-1"
