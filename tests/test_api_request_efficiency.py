import asyncio
from datetime import timezone

import httpx

from kalshi_client.api import KalshiClient
from kalshi_client.models import KalshiMarket
from polymarket_client.api import PolymarketClient
from polymarket_client.models import TokenType


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
    assert metrics == {"requests": 1, "successes": 0, "not_found": 1}


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
