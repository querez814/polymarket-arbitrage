"""
Tests for Polymarket API parsing.
"""

import pytest

from polymarket_client.api import PolymarketClient
from polymarket_client.models import TokenType


@pytest.mark.asyncio
async def test_fetch_token_orderbook_sorts_clob_levels_before_truncating():
    client = PolymarketClient(dry_run=True)

    async def fake_request(method, endpoint, params=None, json_data=None, base_url=None):
        return {
            "bids": [
                {"price": "0.001", "size": "100"},
                {"price": "0.25", "size": "10"},
                {"price": "0.12", "size": "20"},
            ],
            "asks": [
                {"price": "0.999", "size": "100"},
                {"price": "0.08", "size": "10"},
                {"price": "0.15", "size": "20"},
            ],
        }

    client._request = fake_request

    book = await client._fetch_token_orderbook("token", TokenType.YES)

    assert book.best_bid == 0.25
    assert book.best_ask == 0.08
    assert [level.price for level in book.bids.levels] == [0.25, 0.12, 0.001]
    assert [level.price for level in book.asks.levels] == [0.08, 0.15, 0.999]
