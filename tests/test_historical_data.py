import json
from datetime import datetime, timezone

import pytest

from kalshi_client import KalshiClient
from scripts.fetch_historical_data import _filter_history_range
from polymarket_client import PolymarketClient
from utils.historical_data import (
    normalize_kalshi_candlesticks,
    normalize_polymarket_history,
    parse_timestamp,
    to_unix_seconds,
    write_jsonl,
)


def test_parse_timestamp_normalizes_to_utc():
    parsed = parse_timestamp("2026-06-24T12:30:00Z")

    assert parsed == datetime(2026, 6, 24, 12, 30, tzinfo=timezone.utc)
    assert to_unix_seconds(parsed) == 1782304200


def test_normalize_polymarket_history_shape():
    records = normalize_polymarket_history(
        market_id="123",
        token="YES",
        token_id="token-yes",
        history=[{"t": 1782304200, "p": "0.42"}],
    )

    assert records == [{
        "timestamp": "2026-06-24T12:30:00Z",
        "platform": "polymarket",
        "market_id": "123",
        "token": "YES",
        "price": 0.42,
        "bid": None,
        "ask": None,
        "volume": None,
        "source": "prices-history",
        "raw": {
            "token_id": "token-yes",
            "point": {"t": 1782304200, "p": "0.42"},
        },
    }]


def test_normalize_kalshi_candlesticks_supports_live_shape():
    records = normalize_kalshi_candlesticks(
        market_id="KXTEST",
        source="candlesticks",
        candlesticks=[{
            "end_period_ts": 1782304200,
            "yes_bid": {"close_dollars": "0.4100"},
            "yes_ask": {"close_dollars": "0.4300"},
            "price": {"close_dollars": "0.4200"},
            "volume_fp": "12.50",
        }],
    )

    assert records[0]["platform"] == "kalshi"
    assert records[0]["market_id"] == "KXTEST"
    assert records[0]["price"] == 0.42
    assert records[0]["bid"] == 0.41
    assert records[0]["ask"] == 0.43
    assert records[0]["volume"] == 12.5


def test_write_jsonl_creates_parent_and_writes_records(tmp_path):
    path = tmp_path / "nested" / "history.jsonl"

    count = write_jsonl(path, [{"timestamp": "2026-06-24T12:30:00Z", "platform": "test"}])

    assert count == 1
    assert json.loads(path.read_text().strip())["platform"] == "test"


def test_filter_history_range_discards_upstream_out_of_range_points():
    history = [
        {"t": 9, "p": 0.1},
        {"t": 10, "p": 0.2},
        {"t": 20, "p": 0.3},
        {"t": 21, "p": 0.4},
    ]

    assert _filter_history_range(history, 10, 20) == [
        {"t": 10, "p": 0.2},
        {"t": 20, "p": 0.3},
    ]


@pytest.mark.asyncio
async def test_polymarket_get_prices_history_parses_history(monkeypatch):
    client = PolymarketClient(dry_run=True)
    seen = {}

    async def fake_request(method, endpoint, params=None, json_data=None, base_url=None):
        seen.update({
            "method": method,
            "endpoint": endpoint,
            "params": params,
            "base_url": base_url,
        })
        return {"history": [{"t": 1782304200, "p": 0.42}]}

    monkeypatch.setattr(client, "_request", fake_request)

    history = await client.get_prices_history(
        token_id="token-yes",
        start_ts=1,
        end_ts=2,
        fidelity=60,
        interval="1d",
    )

    assert history == [{"t": 1782304200, "p": 0.42}]
    assert seen["endpoint"] == "/prices-history"
    assert seen["params"] == {
        "market": "token-yes",
        "startTs": 1,
        "endTs": 2,
        "fidelity": 60,
        "interval": "1d",
    }


@pytest.mark.asyncio
async def test_kalshi_candlestick_methods_parse_candlesticks(monkeypatch):
    client = KalshiClient(dry_run=True)
    calls = []

    async def fake_get(endpoint, params=None):
        calls.append((endpoint, params))
        return {"candlesticks": [{"end_period_ts": 1782304200}]}

    monkeypatch.setattr(client, "_get", fake_get)

    current = await client.get_market_candlesticks(
        series_ticker="KXSERIES",
        ticker="KXMARKET",
        start_ts=1,
        end_ts=2,
        period_interval=60,
    )
    archived = await client.get_historical_market_candlesticks(
        ticker="KXMARKET",
        start_ts=1,
        end_ts=2,
        period_interval=60,
    )

    assert current == [{"end_period_ts": 1782304200}]
    assert archived == [{"end_period_ts": 1782304200}]
    assert calls[0][0] == "/series/KXSERIES/markets/KXMARKET/candlesticks"
    assert calls[1][0] == "/historical/markets/KXMARKET/candlesticks"


@pytest.mark.asyncio
async def test_kalshi_list_historical_markets_parses_archived_shape(monkeypatch):
    client = KalshiClient(dry_run=True)

    async def fake_get(endpoint, params=None):
        assert endpoint == "/historical/markets"
        assert params == {"limit": 2}
        return {
            "cursor": "next",
            "markets": [{
                "ticker": "KXARCHIVED",
                "event_ticker": "KXEVENT",
                "title": "Archived market",
                "status": "finalized",
                "volume_fp": "12.00",
                "open_interest_fp": "3.00",
            }],
        }

    monkeypatch.setattr(client, "_get", fake_get)

    markets, cursor = await client.list_historical_markets(max_markets=2)

    assert cursor == "next"
    assert markets[0].ticker == "KXARCHIVED"
    assert markets[0].volume == 12
    assert markets[0].open_interest == 3
