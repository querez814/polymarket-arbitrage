import asyncio
from types import SimpleNamespace

from core.data_feed import DataFeed
from polymarket_client.models import Market, OrderBook


def _market(market_id: str, *, liquidity: float, volume_24h: float) -> Market:
    return Market(
        market_id=market_id,
        condition_id=f"condition-{market_id}",
        question=f"Will {market_id} happen?",
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        liquidity=liquidity,
        volume_24h=volume_24h,
    )


def _config(*, min_volume=50.0, resync_seconds=1800.0, broad_orderbook_stream=True):
    return SimpleNamespace(
        use_simulation=False,
        mode=SimpleNamespace(
            semantic_min_polymarket_liquidity=0.0,
            semantic_min_polymarket_volume_24h=min_volume,
            polymarket_market_resync_seconds=resync_seconds,
            polymarket_priority_refresh_seconds=0.01,
            polymarket_broad_orderbook_stream_enabled=broad_orderbook_stream,
        ),
    )


def test_discovery_uses_semantic_volume_threshold():
    class Client:
        async def list_markets(self, _filters):
            return [
                _market("high-volume", liquidity=0, volume_24h=50),
                _market("depth-but-no-volume", liquidity=10_000, volume_24h=0),
                _market("below-volume", liquidity=10_000, volume_24h=49),
            ]

    async def exercise():
        feed = DataFeed(Client(), [], config=_config())
        await feed._fetch_markets()
        return feed

    feed = asyncio.run(exercise())

    assert feed.market_ids == ["high-volume"]
    assert set(feed._markets) == {"high-volume"}


def test_resync_atomically_prunes_markets_and_stale_books():
    snapshots = [
        [
            _market("still-open", liquidity=100, volume_24h=50),
            _market("will-close", liquidity=100, volume_24h=50),
        ],
        [
            _market("still-open", liquidity=100, volume_24h=50),
            _market("new-market", liquidity=100, volume_24h=50),
        ],
    ]

    class Client:
        async def list_markets(self, _filters):
            return snapshots.pop(0)

    async def exercise():
        feed = DataFeed(Client(), [], config=_config())
        await feed._fetch_markets()
        feed._order_books["will-close"] = OrderBook(market_id="will-close")
        await feed.resync_markets(restart_stream=False)
        return feed

    feed = asyncio.run(exercise())

    assert feed.market_ids == ["still-open", "new-market"]
    assert set(feed._markets) == {"still-open", "new-market"}
    assert "will-close" not in feed._order_books


def test_priority_lane_keeps_matched_market_books_fresh():
    updated = asyncio.Event()

    class Client:
        async def get_orderbook(self, market_id):
            updated.set()
            return OrderBook(market_id=market_id)

    async def exercise():
        feed = DataFeed(Client(), [], config=_config())
        feed._markets = {"matched": _market("matched", liquidity=0, volume_24h=50)}
        feed.market_ids = ["matched"]
        feed._running = True
        feed.set_priority_markets(["matched"])
        await asyncio.wait_for(updated.wait(), timeout=1)
        feed._running = False
        if feed._priority_orderbook_task:
            feed._priority_orderbook_task.cancel()
            try:
                await feed._priority_orderbook_task
            except asyncio.CancelledError:
                pass
        return feed

    feed = asyncio.run(exercise())

    assert "matched" in feed._order_books


def test_priority_groups_merge_without_one_strategy_erasing_another():
    feed = DataFeed(object(), [], config=_config())
    feed._markets = {
        "locked-arb": _market("locked-arb", liquidity=100, volume_24h=50),
        "reaction": _market("reaction", liquidity=100, volume_24h=50),
    }

    feed.set_priority_market_group("locked_arb", ["locked-arb"])
    feed.set_priority_market_group("platform_opportunity", ["reaction"])
    feed.set_priority_market_group("locked_arb", [])

    assert feed._priority_market_ids == {"reaction"}


def test_broad_orderbook_stream_can_be_disabled_for_pair_scoped_paper_runtime():
    called = False

    class Client:
        async def stream_orderbook(self, market_ids, use_simulation=False):
            nonlocal called
            called = True
            yield "unexpected", OrderBook(market_id="unexpected")

    async def exercise():
        feed = DataFeed(
            Client(),
            ["market-1"],
            config=_config(broad_orderbook_stream=False),
        )
        feed._running = True
        task = asyncio.create_task(feed._stream_orderbooks())
        await asyncio.sleep(0)
        feed._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())

    assert called is False
