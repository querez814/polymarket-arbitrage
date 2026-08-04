from __future__ import annotations

import asyncio
import ssl
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.news_catalyst import (
    NewsBatch,
    NewsCatalystMapper,
    NewsCatalystService,
    NewsItem,
    OpenAINewsProvider,
    PairRankingInput,
    TrackedMarket,
    rank_pairs,
)
from utils.paper_trade_store import PaperTradeStore


class FakeEmbedder:
    cache_hits = 0
    cache_misses = 0

    async def embed_many(self, texts):
        vectors = {
            "LeBron ownership vote\nLeague owners meet today.": [1.0, 0.0],
            "Will LeBron become an NBA team owner?": [1.0, 0.0],
            "Will bitcoin exceed $200,000?": [0.0, 1.0],
        }
        return [vectors[text] for text in texts]


@pytest.mark.asyncio
async def test_mapper_compares_news_with_all_markets_without_category_partition():
    item = NewsItem(
        headline="LeBron ownership vote",
        summary="League owners meet today.",
        entities=["LeBron James", "NBA"],
        topic_category="finance",
        published_at=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
        source_url="https://example.com/lebron",
    )
    markets = [
        TrackedMarket(
            platform="polymarket",
            market_id="poly-lebron",
            question="Will LeBron become an NBA team owner?",
            volume=1000,
        ),
        TrackedMarket(
            platform="kalshi",
            market_id="kxbtc",
            question="Will bitcoin exceed $200,000?",
            volume=2000,
        ),
    ]

    matches = await NewsCatalystMapper(
        embedder=FakeEmbedder(), similarity_threshold=0.60
    ).match([item], markets)

    assert [(match.market_id, match.relevance_score) for match in matches] == [
        ("poly-lebron", 1.0)
    ]


@pytest.mark.asyncio
async def test_mapper_keeps_event_loop_responsive_during_matrix_scoring(
    monkeypatch,
):
    import core.news_catalyst as news_module

    original = news_module._matrix_matches

    def delayed_matrix(*args):
        time.sleep(0.05)
        return original(*args)

    monkeypatch.setattr(news_module, "_matrix_matches", delayed_matrix)
    item = NewsItem(
        headline="LeBron ownership vote",
        summary="League owners meet today.",
        entities=["LeBron James"],
        topic_category="sports",
        published_at=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
        source_url="https://example.com/lebron",
    )
    mapper = NewsCatalystMapper(
        embedder=FakeEmbedder(), similarity_threshold=0.60
    )
    ticked = asyncio.Event()

    async def tick():
        await asyncio.sleep(0.01)
        ticked.set()

    mapping = asyncio.create_task(
        mapper.match(
            [item],
            [
                TrackedMarket(
                    platform="polymarket",
                    market_id="poly-lebron",
                    question="Will LeBron become an NBA team owner?",
                    volume=1000,
                )
            ],
        )
    )
    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.02)
    assert ticked.is_set()
    await asyncio.gather(mapping, ticker)


def test_rank_pairs_applies_news_boost_to_volume_weighted_similarity():
    ranked = rank_pairs(
        [
            PairRankingInput(
                polymarket_id="quiet",
                kalshi_ticker="quiet-k",
                similarity_score=0.95,
                polymarket_volume=1000,
                kalshi_volume=1000,
            ),
            PairRankingInput(
                polymarket_id="catalyst",
                kalshi_ticker="catalyst-k",
                similarity_score=0.90,
                polymarket_volume=1000,
                kalshi_volume=1000,
            ),
        ],
        news_scores={
            ("polymarket", "catalyst"): 1.0,
            ("kalshi", "catalyst-k"): 0.8,
        },
        catalyst_boost_weight=0.35,
    )

    assert [row.polymarket_id for row in ranked] == ["catalyst", "quiet"]
    assert ranked[0].news_relevance_score == 1.0
    assert ranked[0].rank_score > ranked[1].rank_score


@pytest.mark.asyncio
async def test_openai_provider_discards_model_url_not_returned_by_web_search():
    parsed = NewsBatch(
        items=[
            NewsItem(
                headline="Verified",
                summary="Backed by a fetched page.",
                entities=["Example"],
                topic_category="tech",
                published_at=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
                source_url="https://news.example/verified",
            ),
            NewsItem(
                headline="Invented source",
                summary="The model supplied a URL that search never returned.",
                entities=["Example"],
                topic_category="tech",
                published_at=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
                source_url="https://fake.example/invented",
            ),
        ]
    )
    response = SimpleNamespace(
        output_parsed=parsed,
        model_dump=lambda: {
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [{"url": "https://news.example/verified"}]
                    },
                }
            ]
        },
    )

    class FakeResponses:
        async def parse(self, **kwargs):
            self.kwargs = kwargs
            return response

    fake_client = SimpleNamespace(responses=FakeResponses())
    provider = OpenAINewsProvider(
        api_key="test",
        model="gpt-5.6",
        client=fake_client,
    )

    items = await provider.fetch_recent(lookback_hours=6, max_items=40)

    assert [str(item.source_url) for item in items] == [
        "https://news.example/verified"
    ]
    assert fake_client.responses.kwargs["tools"] == [
        {"type": "web_search", "search_context_size": "low"}
    ]
    assert fake_client.responses.kwargs["tool_choice"] == "required"


def test_store_enforces_durable_daily_call_cap_and_persists_auditable_matches(
    tmp_path,
):
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    now = datetime(2026, 7, 23, 15, tzinfo=timezone.utc)
    try:
        first = store.reserve_news_api_call(max_daily_calls=1, called_at=now)
        assert first is not None
        assert store.reserve_news_api_call(max_daily_calls=1, called_at=now) is None
        store.complete_news_api_call(first, status="succeeded", item_count=1)

        item = NewsItem(
            headline="Fed decision",
            summary="The central bank published its decision.",
            entities=["Federal Reserve"],
            topic_category="finance",
            published_at=datetime(2026, 7, 23, 14, tzinfo=timezone.utc),
            source_url="https://news.example/fed",
        )
        store.record_news_catalysts(
            [item],
            [
                SimpleNamespace(
                    news_index=0,
                    market_platform="kalshi",
                    market_id="KXFED",
                    relevance_score=0.91,
                )
            ],
            scanned_at=now,
        )

        rows = store.recent_news_catalysts(limit=10)
        assert rows[0]["headline"] == "Fed decision"
        assert rows[0]["matches"] == [
            {
                "market_platform": "kalshi",
                "market_id": "KXFED",
                "relevance_score": 0.91,
                "matched_at_utc": "2026-07-23T15:00:00Z",
            }
        ]
        assert store.news_api_calls_today(at=now) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_service_skips_provider_when_durable_daily_cap_is_reached(tmp_path):
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    now = datetime(2026, 7, 23, 15, tzinfo=timezone.utc)

    class Provider:
        calls = 0

        async def fetch_recent(self, **kwargs):
            self.calls += 1
            return []

    provider = Provider()
    service = NewsCatalystService(
        provider=provider,
        mapper=NewsCatalystMapper(
            embedder=FakeEmbedder(), similarity_threshold=0.60
        ),
        store=store,
        lookback_hours=6,
        max_items=40,
        max_daily_api_calls=2,
        clock=lambda: now,
    )
    try:
        first = await service.run_once([])
        second = await service.run_once([])
    finally:
        store.close()

    assert first.status == "complete"
    assert second.status == "daily_cap_reached"
    assert provider.calls == 1
    assert first.api_calls_today == 2


def test_runtime_does_not_apply_catalyst_ranking_in_log_only_mode():
    from run_with_dashboard import TradingBotWithDashboard

    class Feed:
        def __init__(self):
            self._markets = {
                "quiet": SimpleNamespace(market_id="quiet", volume_24h=1000),
                "catalyst": SimpleNamespace(
                    market_id="catalyst", volume_24h=1000
                ),
            }
            self.applied = None

        def set_priority_markets(self, market_ids):
            self.applied = market_ids

    bot = TradingBotWithDashboard.__new__(TradingBotWithDashboard)
    bot.data_feed = Feed()
    bot._matched_pairs = [
        SimpleNamespace(
            polymarket_id="quiet",
            kalshi_ticker="quiet-k",
            similarity_score=0.95,
        ),
        SimpleNamespace(
            polymarket_id="catalyst",
            kalshi_ticker="catalyst-k",
            similarity_score=0.90,
        ),
    ]
    bot._kalshi_markets = [
        SimpleNamespace(ticker="quiet-k", volume=1000),
        SimpleNamespace(ticker="catalyst-k", volume=1000),
    ]
    bot._news_market_scores = {("polymarket", "catalyst"): 1.0}
    bot._news_market_scores_updated_at = datetime.now(timezone.utc)
    bot.config = SimpleNamespace(
        mode=SimpleNamespace(hot_pair_limit=1),
        news_catalyst=SimpleNamespace(
            catalyst_boost_weight=0.35,
            apply_priority_boost=False,
            lookback_hours=6,
        ),
    )

    bot._update_pair_priority()
    assert bot.data_feed.applied == ["quiet", "catalyst"]

    bot.config.news_catalyst.apply_priority_boost = True
    bot._update_pair_priority()
    assert bot.data_feed.applied == ["catalyst"]

    bot.pair_snapshot_source = object()
    bot._update_pair_priority()
    assert bot.data_feed.applied == []
    del bot.pair_snapshot_source

    bot._news_market_scores_updated_at = datetime.now(timezone.utc) - timedelta(
        hours=7
    )
    bot._update_pair_priority()
    assert bot.data_feed.applied == ["quiet"]
    assert bot._news_market_scores == {}


def test_runtime_marks_transient_news_tls_failure_as_retrying():
    from dashboard.server import dashboard_state
    from run_with_dashboard import TradingBotWithDashboard

    bot = TradingBotWithDashboard.__new__(TradingBotWithDashboard)
    bot._news_market_scores = {("polymarket", "old"): 0.9}
    bot._news_market_scores_updated_at = datetime.now(timezone.utc)
    bot.data_feed = None
    bot.paper_trade_store = SimpleNamespace(
        news_api_calls_today=lambda: 4
    )
    bot.config = SimpleNamespace(
        news_catalyst=SimpleNamespace(scan_interval_seconds=1800)
    )

    delay = bot._handle_news_catalyst_error(
        ssl.SSLError("tls record failed")
    )

    assert delay == 30
    assert dashboard_state.news_catalysts["status"] == "retrying"
    assert dashboard_state.news_catalysts["api_calls_today"] == 4
    assert bot._news_market_scores == {}


def test_runtime_restores_durable_news_api_count_on_startup(monkeypatch):
    from dashboard.server import dashboard_state
    from run_with_dashboard import TradingBotWithDashboard

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    bot = TradingBotWithDashboard.__new__(TradingBotWithDashboard)
    bot.paper_trade_store = SimpleNamespace(
        news_api_calls_today=lambda: 4
    )
    bot._semantic_embedder = FakeEmbedder()
    bot.config = SimpleNamespace(
        mode=SimpleNamespace(semantic_matching_enabled=True),
        news_catalyst=SimpleNamespace(
            enabled=True,
            apply_priority_boost=False,
            model="gpt-5.6-terra",
            relevance_similarity_threshold=0.72,
            lookback_hours=6,
            max_news_items_per_scan=40,
            max_daily_api_calls=60,
            scan_interval_seconds=1800,
        ),
    )

    bot._configure_news_catalyst_scanner()

    assert dashboard_state.news_catalysts["api_calls_today"] == 4
