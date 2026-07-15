import pytest

from kalshi_client import KalshiClient


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
