import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from kalshi_client import KalshiClient
from kalshi_client.models import KalshiMarket


def test_market_parser_preserves_expected_expiration_separately_from_legal_close():
    client = KalshiClient(dry_run=True)

    market = client._parse_market(
        {
            "ticker": "KXSB-27-BUF",
            "title": "Buffalo wins the 2027 Super Bowl?",
            "status": "open",
            "close_time": "2029-02-13T00:00:00Z",
            "expiration_time": "2027-02-15T00:00:00Z",
        }
    )

    assert market is not None
    assert market.close_time == datetime(2029, 2, 13, tzinfo=timezone.utc)
    assert market.expiration_time == datetime(2027, 2, 15, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_list_markets_excludes_multivariate_events_by_default(monkeypatch):
    client = KalshiClient(dry_run=True)
    captured_params = None

    async def fake_get(endpoint, params=None):
        nonlocal captured_params
        assert endpoint == "/markets"
        captured_params = params
        return {"markets": [], "cursor": None}

    monkeypatch.setattr(client, "_get", fake_get)

    await client.list_markets(status="open", limit=1000)

    assert captured_params["mve_filter"] == "exclude"


@pytest.mark.asyncio
async def test_full_catalog_follows_cursor_until_exhausted(monkeypatch):
    client = KalshiClient(dry_run=True)
    calls = []

    async def fake_list_markets(**kwargs):
        calls.append(kwargs)
        ticker = f"KX-{len(calls)}"
        market = KalshiMarket(
            ticker=ticker,
            event_ticker="KXE",
            series_ticker="KXS",
            title=ticker,
        )
        return [market], ("cursor-2" if len(calls) == 1 else None)

    monkeypatch.setattr(client, "list_markets", fake_list_markets)

    markets = await client.list_full_market_catalog(status="open", mve_filter="only")

    assert [market.ticker for market in markets] == ["KX-1", "KX-2"]
    assert calls[1]["cursor"] == "cursor-2"
    assert all(call["mve_filter"] == "only" for call in calls)


@pytest.mark.asyncio
async def test_full_catalog_stops_at_hard_page_budget(monkeypatch):
    client = KalshiClient(dry_run=True)
    calls = 0

    async def fake_list_markets(**_kwargs):
        nonlocal calls
        calls += 1
        market = KalshiMarket(
            ticker=f"KX-{calls}",
            event_ticker="KXE",
            series_ticker="KXS",
            title="Market",
        )
        return [market], f"cursor-{calls}"

    monkeypatch.setattr(client, "list_markets", fake_list_markets)

    markets = await client.list_full_market_catalog(
        max_pages=2,
        max_markets=10,
        max_decoded_bytes=1_000_000,
        wall_time_seconds=5,
    )

    assert len(markets) == 2
    assert client.last_catalog_status["complete"] is False
    assert client.last_catalog_status["stop_reason"] == "page_budget"


@pytest.mark.asyncio
async def test_list_event_markets_preserves_event_context_for_matching(monkeypatch):
    client = KalshiClient(dry_run=True)

    async def fake_get(endpoint, params=None):
        assert endpoint == "/events"
        assert params == {
            "status": "open",
            "limit": 200,
            "with_nested_markets": True,
        }
        return {
            "events": [
                {
                    "event_ticker": "KXMLBGAME-26JUL22-NYYMIL",
                    "series_ticker": "KXMLBGAME",
                    "title": "New York Yankees at Milwaukee Brewers",
                    "category": "Sports",
                    "markets": [
                        {
                            "ticker": "KXMLBGAME-26JUL22-NYYMIL-NYY",
                            "event_ticker": "KXMLBGAME-26JUL22-NYYMIL",
                            "title": "New York Yankees win?",
                            "status": "open",
                        }
                    ],
                }
            ],
            "cursor": "",
        }

    monkeypatch.setattr(client, "_get", fake_get)

    markets, cursor = await client.list_event_markets(status="open")

    assert cursor is None
    assert len(markets) == 1
    assert markets[0].event_title == "New York Yankees at Milwaukee Brewers"
    assert markets[0].category == "Sports"
    assert markets[0].matching_text == (
        "New York Yankees at Milwaukee Brewers — New York Yankees win?"
    )


@pytest.mark.asyncio
async def test_typed_event_catalog_pages_exact_targets_and_deduplicates(monkeypatch):
    client = KalshiClient(dry_run=True)
    calls = []

    async def fake_get(endpoint, params=None):
        assert endpoint == "/events"
        calls.append(params)
        if params.get("cursor") is None:
            return {
                "events": [
                    {
                        "event_ticker": "KXTRUMPSAY-26AUG10",
                        "series_ticker": "KXTRUMPSAY",
                        "title": "Trump says a word",
                        "category": "Politics",
                        "markets": [
                            {"ticker": "KXTRUMPSAY-26AUG10-T1", "title": "one"}
                        ],
                        "milestones": [
                            {
                                "id": "trump-speech",
                                "title": "Speech",
                                "start_date": "2026-08-10T12:00:00Z",
                                "related_event_tickers": ["KXTRUMPSAY-26AUG10"],
                            }
                        ],
                    }
                ],
                "cursor": "next",
            }
        return {
            "events": [
                {
                    "event_ticker": "KXSCRSENS-26",
                    "series_ticker": "KXSCRSENS",
                    "title": "Senate race",
                    "category": "Elections",
                    "markets": [{"ticker": "KXSCRSENS-26-A", "title": "two"}],
                }
            ],
            "milestones": [
                {
                    "id": "trump-speech",
                    "title": "Speech",
                    "start_date": "2026-08-10T12:00:00Z",
                    "related_event_tickers": ["KXTRUMPSAY-26AUG10"],
                }
            ],
        }

    monkeypatch.setattr(client, "_get", fake_get)
    result = await client.list_event_catalog(
        status=None,
        tickers=["KXTRUMPSAY-26AUG10", "KXSCRSENS-26"],
        with_nested_markets=True,
        with_milestones=True,
        limit=2,
    )

    assert result.complete is True
    assert result.stop_reason == "source_exhausted"
    assert result.page_count == 2
    assert [event.event_ticker for event in result.events] == [
        "KXTRUMPSAY-26AUG10",
        "KXSCRSENS-26",
    ]
    assert [market.event_title for market in result.events[0].markets] == [
        "Trump says a word"
    ]
    assert [item.milestone_id for item in result.milestones] == ["trump-speech"]
    assert calls[0]["tickers"] == "KXTRUMPSAY-26AUG10,KXSCRSENS-26"
    assert "status" not in calls[0]


@pytest.mark.asyncio
async def test_typed_event_catalog_rejects_missing_requested_nested_markets(
    monkeypatch,
):
    client = KalshiClient(dry_run=True)
    monkeypatch.setattr(
        client,
        "_get",
        AsyncMock(return_value={"events": [{"event_ticker": "KX-1"}]}),
    )

    with pytest.raises(ValueError, match="nested event markets"):
        await client.list_events_page(with_nested_markets=True)


@pytest.mark.asyncio
async def test_get_fee_schedule_prefers_complete_event_override():
    client = KalshiClient(dry_run=True)
    client._get = AsyncMock(
        side_effect=[
            {"market": {"event_ticker": "EVENT-1", "series_ticker": "SERIES-1"}},
            {
                "event": {
                    "fee_type_override": "quadratic",
                    "fee_multiplier_override": 2,
                }
            },
        ]
    )

    schedule = await client.get_fee_schedule("TICKER-1")

    assert schedule.fee_type == "quadratic"
    assert schedule.fee_multiplier == 2.0
    assert schedule.source == "event"
    assert client._get.await_count == 2


@pytest.mark.asyncio
async def test_get_fee_schedule_reads_series_ticker_from_parent_event():
    client = KalshiClient(dry_run=True)
    client._get = AsyncMock(
        side_effect=[
            {"market": {"event_ticker": "EVENT-1"}},
            {
                "event": {
                    "series_ticker": "SERIES-1",
                    "fee_type_override": None,
                    "fee_multiplier_override": None,
                }
            },
            {"series": {"fee_type": "quadratic", "fee_multiplier": 1.5}},
        ]
    )

    schedule = await client.get_fee_schedule("TICKER-1")

    assert schedule.fee_type == "quadratic"
    assert schedule.fee_multiplier == 1.5
    assert schedule.source == "series"
    assert [call.args[0] for call in client._get.await_args_list] == [
        "/markets/TICKER-1",
        "/events/EVENT-1",
        "/series/SERIES-1",
    ]


@pytest.mark.asyncio
async def test_kalshi_get_orderbook_supports_fractional_dollar_shape(monkeypatch):
    client = KalshiClient(dry_run=True)

    async def fake_get(endpoint, params=None):
        assert endpoint == "/markets/KXTEST/orderbook"
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.5100", "10.5"], ["0.5200", "3.25"]],
                "no_dollars": [["0.4800", "4"], ["0.4900", "2"]],
            }
        }

    monkeypatch.setattr(client, "_get", fake_get)

    orderbook = await client.get_orderbook("KXTEST")
    unified = orderbook.to_unified_orderbook()

    assert orderbook.best_bid_yes == 0.52
    assert orderbook.best_bid_no == 0.49
    assert unified.best_bid_yes == 0.52
    assert unified.best_ask_yes == 0.51
    assert [level.size for level in unified.yes.asks.levels] == [2.0, 4.0]


@pytest.mark.asyncio
async def test_kalshi_get_orderbook_handles_empty_fixed_point_sides(monkeypatch):
    client = KalshiClient(dry_run=True)

    async def fake_get(endpoint, params=None):
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}

    monkeypatch.setattr(client, "_get", fake_get)

    orderbook = await client.get_orderbook("KXEMPTY")

    assert orderbook is not None
    assert orderbook.best_bid_yes is None
    assert orderbook.best_ask_yes is None
    assert orderbook.to_unified_orderbook().yes.asks.levels == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"orderbook": {"yes": [[51, 4]], "no": [[48, 2]]}},
        {"orderbook_fp": {"yes_dollars": []}},
        {
            "orderbook_fp": {
                "yes_dollars": [["0.51", "4"], ["0.50", "2"]],
                "no_dollars": [],
            }
        },
        {
            "orderbook_fp": {
                "yes_dollars": [[0.51, "4"]],
                "no_dollars": [],
            }
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["NaN", "4"]],
                "no_dollars": [],
            }
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["5.1e-1", "4"]],
                "no_dollars": [],
            }
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.51", "0"]],
                "no_dollars": [],
            }
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.51"]],
                "no_dollars": [],
            }
        },
    ],
)
async def test_kalshi_get_orderbook_rejects_noncanonical_depth(monkeypatch, response):
    client = KalshiClient(dry_run=True)

    async def fake_get(endpoint, params=None):
        return response

    monkeypatch.setattr(client, "_get", fake_get)

    assert await client.get_orderbook("KXINVALID") is None
