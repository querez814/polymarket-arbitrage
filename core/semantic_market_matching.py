"""Three-stage semantic alignment for cross-venue prediction markets.

The external interface is deliberately small: provide two active venue snapshots and
receive verified equivalences plus a review queue. Structural filtering, embedding
cache behavior, retrieval, and resolution-semantics verification remain internal so
callers cannot treat embedding proximity as trade authorization.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import struct
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, Sequence

import httpx


class SemanticRelation(str, Enum):
    EQUIVALENT = "equivalent"
    SUBSET = "subset"
    SUPERSET = "superset"
    INDEPENDENT = "independent"


@dataclass(frozen=True)
class MarketDocument:
    venue: str
    market_id: str
    execution_id: str
    title: str
    semantic_text: str
    category: str
    opens_at: datetime | None
    closes_at: datetime | None
    source: Any


@dataclass(frozen=True)
class Verification:
    polymarket: MarketDocument
    kalshi: MarketDocument
    retrieval_score: float
    relation: SemanticRelation
    confidence: float
    reasons: tuple[str, ...]
    auto_approved: bool


@dataclass(frozen=True)
class PipelineMetrics:
    polymarket_markets: int
    kalshi_markets: int
    eligible_polymarket_markets: int
    eligible_kalshi_markets: int
    filtered_polymarket_markets: int
    filtered_kalshi_markets: int
    structural_candidates: int
    retrieved_candidates: int
    verified_candidates: int
    embedding_cache_hits: int
    embedding_cache_misses: int


@dataclass(frozen=True)
class PipelineResult:
    verified: list[Verification]
    review: list[Verification]
    metrics: PipelineMetrics


class Embedder(Protocol):
    cache_hits: int
    cache_misses: int

    async def embed_many(self, texts: Sequence[str]) -> list[list[float]]: ...


class ResolutionVerifier(Protocol):
    async def verify_many(
        self,
        candidates: Sequence[tuple[MarketDocument, MarketDocument, float]],
    ) -> list[tuple[SemanticRelation, float, tuple[str, ...]]]: ...


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _category(value: Any, text: str) -> str:
    raw = str(value or "").strip().casefold()
    aliases = {
        "political": "politics",
        "economics": "finance",
        "technology": "tech",
        "sport": "sports",
    }
    if raw:
        return aliases.get(raw, raw)
    lowered = text.casefold()
    groups = {
        "politics": ("election", "president", "senate", "congress", "governor", "mayor"),
        "crypto": ("bitcoin", "ethereum", "crypto", "solana"),
        "finance": ("interest rate", "inflation", "gdp", "recession", "stock"),
        "sports": (" nfl", " nba", " mlb", " nhl", "world cup", "super bowl"),
        "entertainment": ("oscar", "grammy", "movie", "album", "actor"),
        "tech": ("openai", "artificial intelligence", "nvidia", "spacex"),
    }
    for name, terms in groups.items():
        if any(term in f" {lowered}" for term in terms):
            return name
    return "other"


def polymarket_document(market: Any) -> MarketDocument:
    question = str(getattr(market, "question", "") or "").strip()
    description = str(getattr(market, "description", "") or "").strip()
    resolution = str(getattr(market, "resolution", "") or "").strip()
    tags = " ".join(str(tag) for tag in (getattr(market, "tags", None) or []))
    semantic_text = "\n".join(
        part
        for part in (
            f"Title: {question}",
            f"Description: {description}" if description else "",
            "Outcomes: YES | NO",
            f"Resolution: {resolution}" if resolution else "",
            f"Tags: {tags}" if tags else "",
        )
        if part
    )
    market_id = str(getattr(market, "market_id", ""))
    execution_id = str(getattr(market, "condition_id", "") or market_id)
    return MarketDocument(
        venue="polymarket",
        market_id=market_id,
        execution_id=execution_id,
        title=question,
        semantic_text=semantic_text,
        category=_category(getattr(market, "category", ""), question),
        opens_at=_utc(getattr(market, "created_at", None)),
        closes_at=_utc(getattr(market, "end_date", None)),
        source=market,
    )


def kalshi_document(market: Any) -> MarketDocument:
    title = str(
        getattr(market, "matching_text", "")
        or getattr(market, "title", "")
        or ""
    ).strip()
    subtitle = str(getattr(market, "subtitle", "") or "").strip()
    event_title = str(getattr(market, "event_title", "") or "").strip()
    semantic_text = "\n".join(
        part
        for part in (
            f"Title: {title}",
            f"Event: {event_title}" if event_title and event_title not in title else "",
            f"Description: {subtitle}" if subtitle else "",
            "Outcomes: YES | NO",
        )
        if part
    )
    ticker = str(getattr(market, "ticker", ""))
    return MarketDocument(
        venue="kalshi",
        market_id=ticker,
        execution_id=ticker,
        title=title,
        semantic_text=semantic_text,
        category=_category(getattr(market, "category", ""), title),
        opens_at=None,
        closes_at=_utc(
            getattr(market, "close_time", None)
            or getattr(market, "expiration_time", None)
        ),
        source=market,
    )


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


class LocalSemanticEmbedder:
    """Deterministic offline adapter used by tests and fail-closed diagnostics."""

    _SYNONYMS = {
        "elected": "win",
        "wins": "win",
        "won": "win",
        "victory": "win",
        "presidency": "president",
        "mayoral": "mayor",
    }

    def __init__(self, dimensions: int = 256):
        self.dimensions = dimensions
        self._cache: dict[str, list[float]] = {}
        self.cache_hits = 0
        self.cache_misses = 0

    async def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cached = self._cache.get(digest)
            if cached is not None:
                self.cache_hits += 1
                vectors.append(cached)
                continue
            self.cache_misses += 1
            tokens = [
                self._SYNONYMS.get(token, token)
                for token in re.findall(r"[a-z0-9]+", text.casefold())
            ]
            features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
            vector = [0.0] * self.dimensions
            for feature in features:
                raw = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(raw[:4], "big") % self.dimensions
                sign = 1.0 if raw[4] & 1 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
            self._cache[digest] = vector
            vectors.append(vector)
        return vectors


class SQLiteEmbeddingCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_embeddings (
                    content_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    PRIMARY KEY (content_hash, model, dimensions)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def get(self, digest: str, model: str, dimensions: int) -> list[float] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT vector FROM semantic_embeddings WHERE content_hash=? AND model=? AND dimensions=?",
                (digest, model, dimensions),
            ).fetchone()
        if row is None:
            return None
        payload = bytes(row[0])
        if len(payload) != dimensions * 4:
            return None
        return list(struct.unpack(f"<{dimensions}f", payload))

    def put(self, digest: str, model: str, vector: Sequence[float]) -> None:
        payload = struct.pack(f"<{len(vector)}f", *vector)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO semantic_embeddings
                    (content_hash, model, dimensions, vector, created_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (digest, model, len(vector), payload, now),
            )
            connection.commit()


class OpenAIEmbeddingClient:
    """Batched OpenAI embedding adapter with a persistent content-addressed cache."""

    def __init__(
        self,
        *,
        api_key: str,
        cache_path: str | Path,
        model: str = "text-embedding-3-large",
        dimensions: int = 1024,
        batch_size: int = 128,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key.strip():
            raise ValueError("OpenAI API key is required")
        if dimensions <= 0 or batch_size <= 0:
            raise ValueError("embedding dimensions and batch size must be positive")
        self._api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.batch_size = min(batch_size, 2048)
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self.cache = SQLiteEmbeddingCache(cache_path)
        self.cache_hits = 0
        self.cache_misses = 0

    async def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        digests = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
        vectors: list[list[float] | None] = []
        missing: list[tuple[int, str, str]] = []
        for index, (text, digest) in enumerate(zip(texts, digests)):
            cached = self.cache.get(digest, self.model, self.dimensions)
            vectors.append(cached)
            if cached is None:
                self.cache_misses += 1
                missing.append((index, text, digest))
            else:
                self.cache_hits += 1

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=headers,
            transport=self._transport,
        ) as client:
            for offset in range(0, len(missing), self.batch_size):
                batch = missing[offset : offset + self.batch_size]
                response = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    json={
                        "model": self.model,
                        "input": [item[1] for item in batch],
                        "dimensions": self.dimensions,
                        "encoding_format": "float",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list) or len(data) != len(batch):
                    raise ValueError("OpenAI embeddings response cardinality mismatch")
                by_index = {int(item["index"]): item["embedding"] for item in data}
                for batch_index, (target_index, _text, digest) in enumerate(batch):
                    vector = by_index.get(batch_index)
                    if not isinstance(vector, list) or len(vector) != self.dimensions:
                        raise ValueError("OpenAI embedding has unexpected dimensions")
                    normalized = [float(value) for value in vector]
                    self.cache.put(digest, self.model, normalized)
                    vectors[target_index] = normalized

        if any(vector is None for vector in vectors):
            raise RuntimeError("embedding batch completed with missing vectors")
        return [list(vector) for vector in vectors if vector is not None]


_SCOPE_GROUPS = (
    frozenset({"nomination", "nominee", "primary"}),
    frozenset({"election", "elected", "president", "mayor", "governor"}),
    frozenset({"runoff", "qualify", "qualification"}),
    frozenset({"popular", "electoral"}),
)


def deterministic_verification(
    left: MarketDocument,
    right: MarketDocument,
    retrieval_score: float,
) -> tuple[SemanticRelation, float, tuple[str, ...]]:
    left_tokens = set(re.findall(r"[a-z0-9]+", left.semantic_text.casefold()))
    right_tokens = set(re.findall(r"[a-z0-9]+", right.semantic_text.casefold()))
    reasons: list[str] = []
    for group in _SCOPE_GROUPS:
        left_scope = left_tokens & group
        right_scope = right_tokens & group
        if bool(left_scope) != bool(right_scope):
            reasons.append("scope_conflict")
            break
    left_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", left.title))
    right_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", right.title))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        reasons.append("numeric_or_date_conflict")
    if reasons:
        return SemanticRelation.INDEPENDENT, min(0.89, retrieval_score), tuple(reasons)

    shared = left_tokens & right_tokens
    union = left_tokens | right_tokens
    lexical = len(shared) / len(union) if union else 0.0
    confidence = min(0.97, 0.65 * retrieval_score + 0.35 * lexical + 0.36)
    if confidence >= 0.80:
        return SemanticRelation.EQUIVALENT, confidence, ("deterministic_semantic_agreement",)
    return SemanticRelation.INDEPENDENT, confidence, ("insufficient_resolution_evidence",)


class OpenAIResolutionVerifier:
    """Schema-constrained two-pass resolution-semantics verifier."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5.6-sol",
        batch_size: int = 12,
        timeout_seconds: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key.strip():
            raise ValueError("OpenAI API key is required")
        self._api_key = api_key
        self.model = model
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    async def verify_many(
        self,
        candidates: Sequence[tuple[MarketDocument, MarketDocument, float]],
    ) -> list[tuple[SemanticRelation, float, tuple[str, ...]]]:
        results: list[tuple[SemanticRelation, float, tuple[str, ...]]] = []
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=headers,
            transport=self._transport,
        ) as client:
            for offset in range(0, len(candidates), self.batch_size):
                batch = candidates[offset : offset + self.batch_size]
                records = [
                    {
                        "id": index,
                        "polymarket": left.semantic_text,
                        "polymarket_close": left.closes_at.isoformat() if left.closes_at else None,
                        "kalshi": right.semantic_text,
                        "kalshi_close": right.closes_at.isoformat() if right.closes_at else None,
                    }
                    for index, (left, right, _score) in enumerate(batch)
                ]
                schema = {
                    "type": "object",
                    "properties": {
                        "pairs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer"},
                                    "plausible": {"type": "boolean"},
                                    "relation": {
                                        "type": "string",
                                        "enum": ["equivalent", "subset", "superset", "independent"],
                                    },
                                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                    "reasons": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["id", "plausible", "relation", "confidence", "reasons"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["pairs"],
                    "additionalProperties": False,
                }
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    json={
                        "model": self.model,
                        "store": False,
                        "reasoning": {"effort": "medium"},
                        "instructions": (
                            "Classify prediction-market payoff relations. First reject clearly "
                            "incompatible propositions. Then compare exact YES regions using title, "
                            "resolution text, cutoff, oracle/source, dispute rules, and scope. "
                            "Equivalent means identical payout in every state. Subset means the "
                            "Polymarket YES region strictly implies Kalshi YES; superset is reverse. "
                            "If metadata is missing or ambiguity remains, return independent."
                        ),
                        "input": json.dumps(records, separators=(",", ":")),
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": "market_resolution_relations",
                                "strict": True,
                                "schema": schema,
                            }
                        },
                    },
                )
                response.raise_for_status()
                payload = response.json()
                output_text = payload.get("output_text")
                if not isinstance(output_text, str):
                    output_text = _response_output_text(payload)
                parsed = json.loads(output_text)
                rows = parsed.get("pairs") if isinstance(parsed, dict) else None
                if not isinstance(rows, list) or len(rows) != len(batch):
                    raise ValueError("OpenAI verification response cardinality mismatch")
                indexed = {int(row["id"]): row for row in rows}
                for index in range(len(batch)):
                    row = indexed[index]
                    relation = SemanticRelation(str(row["relation"]))
                    if not bool(row["plausible"]):
                        relation = SemanticRelation.INDEPENDENT
                    confidence = float(row["confidence"])
                    reasons = tuple(str(reason)[:120] for reason in row["reasons"][:8])
                    results.append((relation, confidence, reasons))
        return results


def _response_output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("OpenAI response must be an object")
    for output in payload.get("output", []):
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if isinstance(content, dict) and content.get("type") == "refusal":
                raise ValueError("OpenAI verifier refused the request")
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    return text
    raise ValueError("OpenAI verification response has no output text")


class DeterministicResolutionVerifier:
    async def verify_many(
        self,
        candidates: Sequence[tuple[MarketDocument, MarketDocument, float]],
    ) -> list[tuple[SemanticRelation, float, tuple[str, ...]]]:
        return [deterministic_verification(*candidate) for candidate in candidates]


class SemanticMarketPipeline:
    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        verifier: ResolutionVerifier | None = None,
        top_k: int = 20,
        retrieval_floor: float = 0.55,
        auto_approve_confidence: float = 0.94,
        max_verification_candidates: int = 500,
        min_polymarket_liquidity: float = 0.0,
        min_polymarket_volume_24h: float = 0.0,
        min_kalshi_volume: int = 0,
        min_kalshi_open_interest: int = 0,
    ):
        if top_k <= 0 or max_verification_candidates <= 0:
            raise ValueError("semantic pipeline limits must be positive")
        if not 0 <= retrieval_floor <= 1 or not 0 <= auto_approve_confidence <= 1:
            raise ValueError("semantic pipeline thresholds must be in [0, 1]")
        if min_polymarket_liquidity < 0 or min_polymarket_volume_24h < 0:
            raise ValueError("Polymarket activity thresholds must be non-negative")
        if min_kalshi_volume < 0 or min_kalshi_open_interest < 0:
            raise ValueError("Kalshi activity thresholds must be non-negative")
        self.embedder = embedder or LocalSemanticEmbedder()
        self.verifier = verifier or DeterministicResolutionVerifier()
        self.top_k = top_k
        self.retrieval_floor = retrieval_floor
        self.auto_approve_confidence = auto_approve_confidence
        self.max_verification_candidates = max_verification_candidates
        self.min_polymarket_liquidity = min_polymarket_liquidity
        self.min_polymarket_volume_24h = min_polymarket_volume_24h
        self.min_kalshi_volume = min_kalshi_volume
        self.min_kalshi_open_interest = min_kalshi_open_interest

    async def match(self, polymarket: Sequence[Any], kalshi: Sequence[Any]) -> PipelineResult:
        eligible_poly = [market for market in polymarket if self._polymarket_is_liquid(market)]
        eligible_kalshi = [market for market in kalshi if self._kalshi_is_liquid(market)]
        poly_docs = [polymarket_document(market) for market in eligible_poly]
        kalshi_docs = [kalshi_document(market) for market in eligible_kalshi]
        all_docs = poly_docs + kalshi_docs
        vectors = await self.embedder.embed_many([doc.semantic_text for doc in all_docs])
        poly_vectors = vectors[: len(poly_docs)]
        kalshi_vectors = vectors[len(poly_docs) :]

        kalshi_by_category: dict[str, list[int]] = {}
        token_indexes: dict[str, dict[str, set[int]]] = {}
        for index, document in enumerate(kalshi_docs):
            kalshi_by_category.setdefault(document.category, []).append(index)
            category_index = token_indexes.setdefault(document.category, {})
            for token in _retrieval_tokens(document.title):
                category_index.setdefault(token, set()).add(index)

        structural = 0
        retrieved: list[tuple[MarketDocument, MarketDocument, float]] = []
        for poly_doc, poly_vector in zip(poly_docs, poly_vectors):
            scores: list[tuple[float, MarketDocument]] = []
            category_members = kalshi_by_category.get(poly_doc.category, [])
            category_index = token_indexes.get(poly_doc.category, {})
            candidate_indexes: set[int] = set()
            max_posting = max(20, int(len(category_members) * 0.20))
            for token in _retrieval_tokens(poly_doc.title):
                posting = category_index.get(token, set())
                if len(posting) <= max_posting:
                    candidate_indexes.update(posting)
            for kalshi_index in candidate_indexes:
                kalshi_doc = kalshi_docs[kalshi_index]
                if not _temporal_overlap(poly_doc, kalshi_doc):
                    continue
                structural += 1
                score = cosine_similarity(poly_vector, kalshi_vectors[kalshi_index])
                if score >= self.retrieval_floor:
                    scores.append((score, kalshi_doc))
                if structural % 5_000 == 0:
                    await asyncio.sleep(0)
            scores.sort(key=lambda item: item[0], reverse=True)
            retrieved.extend(
                (poly_doc, kalshi_doc, score)
                for score, kalshi_doc in scores[: self.top_k]
            )

        retrieved.sort(key=lambda item: item[2], reverse=True)
        to_verify = retrieved[: self.max_verification_candidates]
        verifications = await self.verifier.verify_many(to_verify)
        verified: list[Verification] = []
        review: list[Verification] = []
        for candidate, (relation, confidence, reasons) in zip(to_verify, verifications):
            left, right, score = candidate
            auto_approved = (
                relation is SemanticRelation.EQUIVALENT
                and confidence >= self.auto_approve_confidence
            )
            result = Verification(
                polymarket=left,
                kalshi=right,
                retrieval_score=score,
                relation=relation,
                confidence=confidence,
                reasons=reasons,
                auto_approved=auto_approved,
            )
            if relation is SemanticRelation.EQUIVALENT:
                verified.append(result)
            else:
                review.append(result)

        return PipelineResult(
            verified=verified,
            review=review,
            metrics=PipelineMetrics(
                polymarket_markets=len(polymarket),
                kalshi_markets=len(kalshi),
                eligible_polymarket_markets=len(poly_docs),
                eligible_kalshi_markets=len(kalshi_docs),
                filtered_polymarket_markets=len(polymarket) - len(poly_docs),
                filtered_kalshi_markets=len(kalshi) - len(kalshi_docs),
                structural_candidates=structural,
                retrieved_candidates=len(retrieved),
                verified_candidates=len(to_verify),
                embedding_cache_hits=self.embedder.cache_hits,
                embedding_cache_misses=self.embedder.cache_misses,
            ),
        )

    def _polymarket_is_liquid(self, market: Any) -> bool:
        if self.min_polymarket_liquidity <= 0 and self.min_polymarket_volume_24h <= 0:
            return True
        liquidity = _finite_nonnegative(getattr(market, "liquidity", 0.0))
        volume = _finite_nonnegative(getattr(market, "volume_24h", 0.0))
        return (
            liquidity >= self.min_polymarket_liquidity
            or volume >= self.min_polymarket_volume_24h
        )

    def _kalshi_is_liquid(self, market: Any) -> bool:
        if self.min_kalshi_volume <= 0 and self.min_kalshi_open_interest <= 0:
            return True
        volume = _finite_nonnegative(getattr(market, "volume", 0))
        open_interest = _finite_nonnegative(getattr(market, "open_interest", 0))
        return volume >= self.min_kalshi_volume or open_interest >= self.min_kalshi_open_interest


def _finite_nonnegative(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _temporal_overlap(left: MarketDocument, right: MarketDocument) -> bool:
    if left.closes_at and right.closes_at:
        # Equivalent prediction markets should have materially aligned cutoffs.
        return abs((left.closes_at - right.closes_at).total_seconds()) <= 7 * 86400
    if left.opens_at and right.closes_at and left.opens_at > right.closes_at:
        return False
    if right.opens_at and left.closes_at and right.opens_at > left.closes_at:
        return False
    return True


_RETRIEVAL_NOISE = frozenset(
    {
        "will",
        "the",
        "a",
        "an",
        "be",
        "to",
        "in",
        "on",
        "by",
        "at",
        "yes",
        "no",
        "market",
        "event",
        "win",
        "wins",
        "winner",
        "election",
        "elected",
        "before",
        "after",
        "than",
    }
)


def _retrieval_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.casefold())
        if len(token) >= 3 and token not in _RETRIEVAL_NOISE
    }
