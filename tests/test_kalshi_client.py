import pytest
from unittest.mock import AsyncMock

from kalshi_client import KalshiClient


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
            {"series": {"fee_type": "flat", "fee_multiplier": 9}},
        ]
    )

    schedule = await client.get_fee_schedule("TICKER-1")

    assert schedule.fee_type == "quadratic"
    assert schedule.fee_multiplier == 2.0
    assert schedule.source == "event"


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
