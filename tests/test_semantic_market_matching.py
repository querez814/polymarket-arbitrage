import asyncio
import json
import ssl
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from core.semantic_market_matching import (
    LocalSemanticEmbedder,
    OpenAIEmbeddingClient,
    OpenAIResolutionVerifier,
    SQLiteEmbeddingCache,
    SemanticMarketPipeline,
    SemanticRelation,
    kalshi_document,
    polymarket_document,
)


def test_sqlite_embedding_cache_closes_short_lived_lookup_connections(tmp_path):
    closed: list[bool] = []

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    cache = SQLiteEmbeddingCache(tmp_path / "semantic.db")
    cache._connect = lambda: sqlite3.connect(
        cache.path, factory=TrackingConnection
    )

    assert cache.get("missing", "test-model", 4) is None
    assert closed == [True]


def _poly(question: str, **overrides):
    values = {
        "market_id": "poly-1",
        "condition_id": "condition-1",
        "question": question,
        "description": "Resolves Yes according to the official election result.",
        "category": "politics",
        "active": True,
        "closed": False,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "end_date": datetime(2026, 11, 4, tzinfo=timezone.utc),
        "tags": [],
        "liquidity": 1_000.0,
        "volume_24h": 500.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _kalshi(title: str, **overrides):
    values = {
        "ticker": "KX-ELECTION-1",
        "title": title,
        "matching_text": title,
        "subtitle": "Official election result",
        "event_title": "2026 election",
        "category": "Politics",
        "status": "open",
        "close_time": datetime(2026, 11, 4, tzinfo=timezone.utc),
        "expiration_time": datetime(2026, 11, 4, tzinfo=timezone.utc),
        "volume": 100,
        "open_interest": 50,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_pipeline_verifies_equivalent_paraphrases_for_auto_approval():
    pipeline = SemanticMarketPipeline(auto_approve_confidence=0.92)

    result = asyncio.run(
        pipeline.match(
            [_poly("Will Alice Smith win the 2026 mayoral election?")],
            [_kalshi("Alice Smith elected mayor in 2026?")],
        )
    )

    assert len(result.verified) == 1
    assert result.verified[0].relation is SemanticRelation.EQUIVALENT
    assert result.verified[0].auto_approved is True
    assert result.verified[0].confidence >= 0.92


def test_pipeline_rejects_similar_wording_with_different_resolution_scope():
    pipeline = SemanticMarketPipeline()

    result = asyncio.run(
        pipeline.match(
            [_poly("Will Alice Smith win the 2026 mayoral election?")],
            [_kalshi("Will Alice Smith win the party nomination for mayor in 2026?")],
        )
    )

    assert result.verified == []
    assert result.review
    assert result.review[0].relation is SemanticRelation.INDEPENDENT
    assert "scope_conflict" in result.review[0].reasons


def test_embedding_documents_include_resolution_cutoff_and_oracle_metadata():
    poly = polymarket_document(
        _poly(
            "Will Alice Smith prevail?",
            description="Resolves Yes if Alice Smith is certified mayor.",
            resolution_source="City election board",
        )
    )
    kalshi = kalshi_document(
        _kalshi(
            "Does the candidate prevail?",
            rules_primary="Resolves Yes if Alice Smith is certified mayor.",
            rules_secondary="Certification must be final after recounts.",
            settlement_source="City election board",
        )
    )

    assert "Cutoff: 2026-11-04T00:00:00+00:00" in poly.semantic_text
    assert "Oracle/Source: City election board" in poly.semantic_text
    assert "Resolution criteria: Resolves Yes if Alice Smith" in poly.semantic_text
    assert "Cutoff: 2026-11-04T00:00:00+00:00" in kalshi.semantic_text
    assert "Primary resolution rules: Resolves Yes if Alice Smith" in kalshi.semantic_text
    assert "Secondary resolution rules: Certification must be final" in kalshi.semantic_text
    assert "Oracle/Source: City election board" in kalshi.semantic_text


def test_specific_text_classification_overrides_incorrect_politics_fallback():
    document = kalshi_document(
        _kalshi(
            "Will the New York Yankees win the 2026 World Series?",
            event_title="2026 World Series",
            category="Politics",
        )
    )

    assert document.category == "sports"


def test_unrecognized_text_does_not_inherit_unreliable_politics_default():
    document = kalshi_document(
        _kalshi(
            "Will the Suez Canal close before September?",
            event_title="Suez Canal operations",
            category="Politics",
        )
    )

    assert document.category == "other"


def test_resolution_criteria_can_generate_candidates_for_paraphrased_titles():
    pipeline = SemanticMarketPipeline(retrieval_floor=0.0)
    result = asyncio.run(
        pipeline.match(
            [
                _poly(
                    "Will the official outcome occur?",
                    description="Resolves Yes if Alice Smith is certified mayor.",
                )
            ],
            [
                _kalshi(
                    "Does the candidate prevail?",
                    rules_primary="Resolves Yes if Alice Smith is certified mayor.",
                )
            ],
        )
    )

    assert result.metrics.structural_candidates == 1
    assert result.metrics.retrieved_candidates == 1


def test_pipeline_structurally_excludes_non_overlapping_markets():
    pipeline = SemanticMarketPipeline()

    result = asyncio.run(
        pipeline.match(
            [_poly("Will Alice Smith win the 2026 mayoral election?")],
            [
                _kalshi(
                    "Alice Smith elected mayor in 2024?",
                    close_time=datetime(2024, 11, 4, tzinfo=timezone.utc),
                    expiration_time=datetime(2024, 11, 4, tzinfo=timezone.utc),
                )
            ],
        )
    )

    assert result.verified == []
    assert result.metrics.structural_candidates == 0


def test_pipeline_filters_obviously_inactive_markets_before_embedding():
    embedder = LocalSemanticEmbedder()
    pipeline = SemanticMarketPipeline(
        embedder=embedder,
        min_polymarket_liquidity=1.0,
        min_polymarket_volume_24h=1.0,
        min_kalshi_volume=1,
        min_kalshi_open_interest=1,
    )

    result = asyncio.run(
        pipeline.match(
            [
                _poly("Will Alice Smith win the 2026 mayoral election?"),
                _poly(
                    "Will an inactive market resolve?",
                    market_id="poly-inactive",
                    condition_id="condition-inactive",
                    liquidity=0,
                    volume_24h=0,
                ),
            ],
            [
                _kalshi("Alice Smith elected mayor in 2026?"),
                _kalshi(
                    "Will an inactive market resolve?",
                    ticker="KX-INACTIVE",
                    volume=0,
                    open_interest=0,
                ),
            ],
        )
    )

    assert result.metrics.polymarket_markets == 2
    assert result.metrics.kalshi_markets == 2
    assert result.metrics.eligible_polymarket_markets == 1
    assert result.metrics.eligible_kalshi_markets == 1
    assert result.metrics.filtered_polymarket_markets == 1
    assert result.metrics.filtered_kalshi_markets == 1
    assert embedder.cache_misses == 2


def test_local_embeddings_are_cached_by_unchanged_market_text():
    embedder = LocalSemanticEmbedder()
    pipeline = SemanticMarketPipeline(embedder=embedder)
    poly = _poly("Will Alice Smith win the 2026 mayoral election?")
    kalshi = _kalshi("Alice Smith elected mayor in 2026?")

    asyncio.run(pipeline.match([poly], [kalshi]))
    first_misses = embedder.cache_misses
    asyncio.run(pipeline.match([poly], [kalshi]))

    assert first_misses == 2
    assert embedder.cache_misses == first_misses
    assert embedder.cache_hits >= 2


def test_pipeline_limits_verification_to_top_k_embedding_neighbors():
    pipeline = SemanticMarketPipeline(top_k=3, retrieval_floor=0.0)
    poly = _poly("Will Alice Smith win the 2026 mayoral election?")
    kalshi = [
        _kalshi(
            f"Alice Smith elected mayor in 2026 option {index}?",
            ticker=f"KX-{index}",
        )
        for index in range(20)
    ]

    result = asyncio.run(pipeline.match([poly], kalshi))

    assert result.metrics.retrieved_candidates == 3
    assert result.metrics.verified_candidates == 3


def test_openai_embeddings_are_batched_then_loaded_from_sqlite_cache(tmp_path):
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        inputs = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            request=request,
            json={
                "data": [
                    {"index": index, "embedding": [1.0, 0.0, 0.0, float(index)]}
                    for index, _text in enumerate(inputs)
                ]
            },
        )

    client = OpenAIEmbeddingClient(
        api_key="test-key",
        cache_path=tmp_path / "semantic.db",
        dimensions=4,
        transport=httpx.MockTransport(handler),
    )

    first = asyncio.run(client.embed_many(["alpha", "beta"]))
    second = asyncio.run(client.embed_many(["alpha", "beta"]))

    assert first == second
    assert calls == 1
    assert client.cache_hits == 2
    assert client.cache_misses == 2


def test_openai_embeddings_retry_transient_tls_failure_with_fresh_request(
    tmp_path,
):
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ssl.SSLError("tls record failed")
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
        )

    client = OpenAIEmbeddingClient(
        api_key="test-key",
        cache_path=tmp_path / "semantic.db",
        dimensions=2,
        transport=httpx.MockTransport(handler),
        retry_base_delay=0,
    )

    assert asyncio.run(client.embed_many(["alpha"])) == [[1.0, 0.0]]
    assert calls == 2


def test_openai_verifier_consumes_strict_structured_response():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body["text"]["format"]["strict"] is True
        output = json.dumps(
            {
                "pairs": [
                    {
                        "id": 0,
                        "plausible": True,
                        "relation": "equivalent",
                        "confidence": 0.98,
                        "reasons": ["same cutoff and resolution source"],
                    }
                ]
            }
        )
        return httpx.Response(
            200,
            request=request,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": output}],
                    }
                ]
            },
        )

    left = _poly("Will Alice Smith win the 2026 mayoral election?")
    right = _kalshi("Alice Smith elected mayor in 2026?")
    verifier = OpenAIResolutionVerifier(
        api_key="test-key", transport=httpx.MockTransport(handler)
    )
    from core.semantic_market_matching import kalshi_document, polymarket_document

    result = asyncio.run(
        verifier.verify_many([(polymarket_document(left), kalshi_document(right), 0.9)])
    )

    assert result == [
        (
            SemanticRelation.EQUIVALENT,
            0.98,
            ("same cutoff and resolution source",),
        )
    ]
