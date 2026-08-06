from types import SimpleNamespace

from core.discovery_allocation import (
    DiscoveryCandidate,
    StratifiedVerifierAllocator,
)


def _candidate(index: int, category: str, family: str, score: float):
    return DiscoveryCandidate(
        polymarket=SimpleNamespace(
            market_id=f"p-{index}", category=category, event_family=family
        ),
        kalshi=SimpleNamespace(market_id=f"k-{index}"),
        retrieval_score=score,
        per_market_rank=1,
        global_rank=index + 1,
    )


def test_stratified_allocator_prevents_one_family_from_consuming_the_budget():
    candidates = [
        _candidate(index, "politics", "politics:nominee:2028", 1 - index / 1000)
        for index in range(80)
    ]
    candidates.extend(
        _candidate(100 + index, "finance", f"finance:cpi:{index}", 0.80 - index / 1000)
        for index in range(40)
    )

    result = StratifiedVerifierAllocator(
        category_cap_share=0.60,
        family_cap_share=0.40,
        exploration_share=0.10,
    ).allocate(candidates, limit=50)

    family_counts = result.metrics["stratified_family_counts"]
    assert len(result.selected) == 50
    assert family_counts["politics:nominee:2028"] <= 20
    assert result.metrics["baseline_family_counts"]["politics:nominee:2028"] == 50
    assert result.metrics["selected_category_entropy"] > 0
    assert any(decision.lane == "exploration" for decision in result.decisions)
    assert len(result.verification_sample) == 50
    assert result.metrics["verification_sample_strategy"] == "shadow_union_v1"


def test_unused_strata_flow_back_to_global_rank_without_reducing_budget():
    candidates = [
        _candidate(index, "politics", "politics:only-family", 1 - index / 1000)
        for index in range(30)
    ]

    result = StratifiedVerifierAllocator().allocate(candidates, limit=20)

    assert len(result.selected) == 20
    assert result.metrics["flowback_selected"] > 0


def test_category_cap_prevents_many_political_families_from_saturating_budget():
    candidates = [
        _candidate(index, "politics", f"politics:family:{index}", 1 - index / 1000)
        for index in range(80)
    ]
    candidates.extend(
        _candidate(
            100 + index, "finance", f"finance:family:{index}", 0.8 - index / 1000
        )
        for index in range(40)
    )

    result = StratifiedVerifierAllocator(category_cap_share=0.60).allocate(
        candidates, limit=50
    )

    assert result.metrics["stratified_category_counts"]["politics"] <= 30
    assert result.metrics["stratified_category_counts"]["finance"] >= 20
