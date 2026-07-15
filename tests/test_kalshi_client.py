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
