"""Source-backed news ingestion and market-priority scoring.

This module does not create trades or estimate directional probabilities. It
only records recent, verifiable catalysts and computes an optional monitoring
priority boost for already verified cross-venue pairs.
"""

from __future__ import annotations

import math
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from openai import AsyncOpenAI
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.semantic_market_matching import Embedder

TopicCategory = Literal[
    "politics", "crypto", "finance", "sports", "entertainment", "tech"
]
MarketPlatform = Literal["polymarket", "kalshi"]


class NewsItem(BaseModel):
    """One recent event whose source was fetched by web search."""

    model_config = ConfigDict(frozen=True)

    headline: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=3000)
    entities: list[str] = Field(default_factory=list, max_length=30)
    topic_category: TopicCategory
    published_at: datetime
    source_url: str = Field(min_length=1, max_length=2048)

    @field_validator("headline", "summary")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_url")
    @classmethod
    def _require_http_source(cls, value: str) -> str:
        cleaned = value.strip()
        parts = urlsplit(cleaned)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        if parts.username or parts.password:
            raise ValueError("source_url must not contain credentials")
        return cleaned

    @field_validator("published_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        return value.astimezone(timezone.utc)


class NewsBatch(BaseModel):
    """Structured output contract used by the Responses API."""

    items: list[NewsItem]


@dataclass(frozen=True)
class TrackedMarket:
    platform: MarketPlatform
    market_id: str
    question: str
    volume: float


@dataclass(frozen=True)
class NewsMarketMatch:
    news_index: int
    market_platform: MarketPlatform
    market_id: str
    relevance_score: float


@dataclass(frozen=True)
class PairRankingInput:
    polymarket_id: str
    kalshi_ticker: str
    similarity_score: float
    polymarket_volume: float
    kalshi_volume: float


@dataclass(frozen=True)
class RankedPair:
    polymarket_id: str
    kalshi_ticker: str
    similarity_score: float
    news_relevance_score: float
    rank_score: float


@dataclass(frozen=True)
class NewsScanResult:
    status: Literal["complete", "daily_cap_reached"]
    items: tuple[NewsItem, ...]
    matches: tuple[NewsMarketMatch, ...]
    api_calls_today: int


class NewsProvider(Protocol):
    async def fetch_recent(
        self, *, lookback_hours: int, max_items: int
    ) -> list[NewsItem]: ...


class NewsCatalystService:
    """Own one capped, persisted scan cycle; scheduling remains a caller concern."""

    def __init__(
        self,
        *,
        provider: NewsProvider,
        mapper: "NewsCatalystMapper",
        store: Any,
        lookback_hours: int,
        max_items: int,
        max_daily_api_calls: int,
        clock: Callable[[], datetime] | None = None,
    ):
        self._provider = provider
        self._mapper = mapper
        self._store = store
        self._lookback_hours = lookback_hours
        self._max_items = max_items
        self._max_daily_api_calls = max_daily_api_calls
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def run_once(
        self, markets: Sequence[TrackedMarket]
    ) -> NewsScanResult:
        now = self._clock()
        # One Responses/web-search request plus at most one embedding request:
        # runtime waits for the semantic market cache and caps news at <=128.
        call_ids = self._store.reserve_news_api_calls(
            call_count=2,
            max_daily_calls=self._max_daily_api_calls,
            called_at=now,
        )
        if not call_ids:
            return NewsScanResult(
                status="daily_cap_reached",
                items=(),
                matches=(),
                api_calls_today=self._store.news_api_calls_today(at=now),
            )
        try:
            fetched = await self._provider.fetch_recent(
                lookback_hours=self._lookback_hours,
                max_items=self._max_items,
            )
            cutoff = now.astimezone(timezone.utc) - timedelta(
                hours=self._lookback_hours
            )
            items = [
                item
                for item in fetched[: self._max_items]
                if cutoff <= item.published_at <= now.astimezone(timezone.utc)
            ]
            matches = await self._mapper.match(items, markets)
            self._store.record_news_catalysts(
                items,
                matches,
                scanned_at=now,
            )
            for index, call_id in enumerate(call_ids):
                self._store.complete_news_api_call(
                    call_id,
                    status="succeeded",
                    item_count=len(items) if index == 0 else 0,
                )
            return NewsScanResult(
                status="complete",
                items=tuple(items),
                matches=tuple(matches),
                api_calls_today=self._store.news_api_calls_today(at=now),
            )
        except BaseException as exc:
            for call_id in call_ids:
                self._store.complete_news_api_call(
                    call_id,
                    status="failed",
                    error_detail=f"{type(exc).__name__}: {exc}",
                )
            raise


class NewsCatalystMapper:
    """Map every news item against every tracked market, without categories."""

    def __init__(self, *, embedder: Embedder, similarity_threshold: float):
        if not 0 <= similarity_threshold <= 1:
            raise ValueError("similarity_threshold must be in [0, 1]")
        self._embedder = embedder
        self._threshold = similarity_threshold

    async def match(
        self,
        items: Sequence[NewsItem],
        markets: Sequence[TrackedMarket],
    ) -> list[NewsMarketMatch]:
        if not items or not markets:
            return []
        news_texts = [f"{item.headline}\n{item.summary}" for item in items]
        market_texts = [market.question for market in markets]
        vectors = await self._embedder.embed_many([*news_texts, *market_texts])
        news_vectors = vectors[: len(items)]
        market_vectors = vectors[len(items) :]
        raw_matches = await asyncio.to_thread(
            _matrix_matches,
            news_vectors,
            market_vectors,
            self._threshold,
        )
        matches: list[NewsMarketMatch] = []
        for news_index, market_index, score in raw_matches:
            market = markets[market_index]
            matches.append(
                NewsMarketMatch(
                    news_index=news_index,
                    market_platform=market.platform,
                    market_id=market.market_id,
                    relevance_score=score,
                )
            )
        return sorted(
            matches,
            key=lambda row: (-row.relevance_score, row.market_platform, row.market_id),
        )


def _matrix_matches(
    news_vectors: Sequence[Sequence[float]],
    market_vectors: Sequence[Sequence[float]],
    threshold: float,
) -> list[tuple[int, int, float]]:
    """Vectorized cosine matching, intended to run outside the event-loop thread."""
    if not news_vectors or not market_vectors:
        return []
    news = np.asarray(news_vectors, dtype=np.float32)
    markets = np.asarray(market_vectors, dtype=np.float32)
    if news.ndim != 2 or markets.ndim != 2 or news.shape[1] != markets.shape[1]:
        raise ValueError("news and market embeddings must share one dimension")
    news_norms = np.linalg.norm(news, axis=1, keepdims=True)
    market_norms = np.linalg.norm(markets, axis=1, keepdims=True)
    news = np.divide(news, news_norms, out=np.zeros_like(news), where=news_norms != 0)
    markets = np.divide(
        markets,
        market_norms,
        out=np.zeros_like(markets),
        where=market_norms != 0,
    )
    scores = news @ markets.T
    indices = np.argwhere(scores >= threshold)
    return [
        (int(news_index), int(market_index), float(scores[news_index, market_index]))
        for news_index, market_index in indices
    ]


def rank_pairs(
    pairs: Sequence[PairRankingInput],
    *,
    news_scores: dict[tuple[str, str], float],
    catalyst_boost_weight: float,
) -> list[RankedPair]:
    """Rank verified pairs by similarity, executable-side volume, and catalyst."""
    if catalyst_boost_weight < 0:
        raise ValueError("catalyst_boost_weight must be non-negative")
    ranked: list[RankedPair] = []
    for pair in pairs:
        relevance = max(
            news_scores.get(("polymarket", pair.polymarket_id), 0.0),
            news_scores.get(("kalshi", pair.kalshi_ticker), 0.0),
        )
        minimum_volume = max(
            0.0, min(pair.polymarket_volume, pair.kalshi_volume)
        )
        score = (
            pair.similarity_score
            * math.log1p(minimum_volume)
            * (1 + catalyst_boost_weight * relevance)
        )
        ranked.append(
            RankedPair(
                polymarket_id=pair.polymarket_id,
                kalshi_ticker=pair.kalshi_ticker,
                similarity_score=pair.similarity_score,
                news_relevance_score=relevance,
                rank_score=score,
            )
        )
    return sorted(ranked, key=lambda row: (-row.rank_score, row.polymarket_id))


class OpenAINewsProvider:
    """Fetch structured current news and retain only URLs the search tool returned."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: Any | None = None,
    ):
        if not api_key.strip():
            raise ValueError("OpenAI API key is required")
        self._client = client or AsyncOpenAI(api_key=api_key)
        self._model = model

    async def fetch_recent(
        self, *, lookback_hours: int, max_items: int
    ) -> list[NewsItem]:
        requested_at = datetime.now(timezone.utc)
        response = await self._client.responses.parse(
            model=self._model,
            tools=[{"type": "web_search", "search_context_size": "low"}],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            instructions=(
                "Find recent, verifiable news relevant to prediction markets in "
                "politics, crypto, finance, sports, entertainment, and technology. "
                "Use only pages fetched in this web search. Every item must identify "
                "one fetched page as source_url and include its publication time."
            ),
            input=(
                f"The current UTC time is {requested_at.isoformat()}. Return at "
                f"most {max_items} materially distinct events published "
                f"within the last {lookback_hours} hours. Do not speculate or "
                "include an event without a fetched primary or reputable source."
            ),
            text_format=NewsBatch,
            max_output_tokens=8000,
        )
        parsed = response.output_parsed
        if parsed is None:
            return []
        try:
            payload = response.model_dump(warnings=False)
        except TypeError:
            payload = response.model_dump()
        fetched_urls = _collect_web_search_urls(payload)
        verified: list[NewsItem] = []
        for item in parsed.items[:max_items]:
            if _normalize_url(str(item.source_url)) in fetched_urls:
                verified.append(item)
        return verified


def _normalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, parts.query, "")
    )


def _collect_web_search_urls(payload: Any) -> set[str]:
    """Collect URLs only from web-search sources and URL-citation annotations."""
    urls: set[str] = set()

    def walk(value: Any, *, trusted: bool = False) -> None:
        if isinstance(value, dict):
            kind = str(value.get("type", ""))
            next_trusted = trusted or kind in {
                "web_search_call",
                "url_citation",
            }
            if next_trusted and isinstance(value.get("url"), str):
                urls.add(_normalize_url(value["url"]))
            for key, child in value.items():
                child_trusted = next_trusted or (
                    trusted and key in {"sources", "annotations"}
                )
                walk(child, trusted=child_trusted)
        elif isinstance(value, list):
            for child in value:
                walk(child, trusted=trusted)

    walk(payload)
    return urls
