import asyncio
from types import SimpleNamespace

import pytest

from core.cross_platform_arb import MarketMatcher


@pytest.mark.parametrize(
    ("polymarket_question", "kalshi_title"),
    [
        (
            "LoL: ZYB Esport vs Skillcamp Esport",
            "yes Wilyer Abreu: 1+, yes Brice Turang: 1+",
        ),
        (
            "Clarity Act signed into law in 2026",
            "yes Breanna Stewart: 15+, yes Jonquel Jones: 10+",
        ),
        (
            "Iran withdraws from the nuclear treaty in 2026",
            "yes Logan Henderson: 5+, yes Seattle Mariners: 2+",
        ),
        (
            "Harry Kane wins the Ballon d'Or",
            "yes Seattle Mariners, yes Logan Henderson",
        ),
        (
            "Pittsburgh Pirates vs. New York Yankees",
            "yes Pittsburgh, yes New York Y wins 2+, yes Aaron Judge 1+",
        ),
        (
            "New York Mets vs. Milwaukee Brewers",
            "yes New York Y, yes New York Y, yes Milwaukee B 2+",
        ),
    ],
)
def test_unrelated_markets_never_receive_a_high_confidence_match(
    polymarket_question: str,
    kalshi_title: str,
):
    matcher = MarketMatcher(min_similarity=0.90)

    assert matcher.calculate_similarity(polymarket_question, kalshi_title) < 0.90


def test_exact_sports_match_remains_high_confidence():
    matcher = MarketMatcher(min_similarity=0.90)

    score = matcher.calculate_similarity(
        "Pittsburgh Steelers vs Baltimore Ravens",
        "Baltimore Ravens at Pittsburgh Steelers",
    )

    assert score >= 0.90


def test_team_abbreviations_require_whole_tokens():
    matcher = MarketMatcher()

    assert matcher.extract_teams("indicator miniature tenacious") == []
    assert set(matcher.extract_teams("IND vs CAR")) == {
        "indianapolis colts",
        "carolina panthers",
    }


def test_city_names_alone_are_not_team_identities():
    matcher = MarketMatcher()

    assert matcher.extract_teams("Pittsburgh and New York") == []


def test_category_detection_does_not_use_team_abbreviation_substrings():
    matcher = MarketMatcher()

    assert matcher._categorize_market("A new technology launches") == "other"
    assert matcher._categorize_market("A new treaty is signed") == "other"


def test_bulk_matching_yields_to_other_async_work():
    async def exercise_matcher():
        matcher = MarketMatcher(min_similarity=2.0)
        shared_markers = " ".join(f"marker{index}" for index in range(100))
        polymarket_markets = [
            SimpleNamespace(
                active=True,
                question=shared_markers,
                market_id=f"poly-{index}",
                condition_id=f"condition-{index}",
                category="sports",
            )
            for index in range(1)
        ]
        kalshi_markets = [
            SimpleNamespace(
                is_active=True,
                title=f"marker{index % 100}",
                ticker=f"kalshi-{index}",
                category="sports",
            )
            for index in range(600)
        ]
        heartbeat_ran = False

        async def heartbeat():
            nonlocal heartbeat_ran
            await asyncio.sleep(0)
            heartbeat_ran = True

        heartbeat_task = asyncio.create_task(heartbeat())
        await matcher.find_matches(polymarket_markets, kalshi_markets)
        heartbeat_ran_before_completion = heartbeat_ran
        await heartbeat_task
        return heartbeat_ran_before_completion

    assert asyncio.run(exercise_matcher()) is True


def test_matching_uses_parent_event_context_from_kalshi():
    async def find_pair():
        matcher = MarketMatcher(min_similarity=0.80)
        polymarket = SimpleNamespace(
            active=True,
            question="Pittsburgh Steelers at Baltimore Ravens — Steelers win?",
            market_id="poly-1",
            condition_id="condition-1",
        )
        kalshi = SimpleNamespace(
            is_active=True,
            title="Steelers win?",
            matching_text=("Pittsburgh Steelers at Baltimore Ravens — Steelers win?"),
            ticker="kalshi-1",
        )
        return await matcher.find_matches([polymarket], [kalshi])

    pairs = asyncio.run(find_pair())

    assert len(pairs) == 1
    assert pairs[0].kalshi_title == (
        "Pittsburgh Steelers at Baltimore Ravens — Steelers win?"
    )


def test_matching_uses_venue_category_metadata_for_sports_without_static_team_map():
    async def find_pair():
        matcher = MarketMatcher(min_similarity=0.90)
        polymarket = SimpleNamespace(
            active=True,
            question="New York Yankees at Milwaukee Brewers — Yankees win?",
            market_id="poly-1",
            condition_id="condition-1",
            category="sports",
        )
        kalshi = SimpleNamespace(
            is_active=True,
            title="Yankees win?",
            matching_text=("New York Yankees at Milwaukee Brewers — Yankees win?"),
            ticker="kalshi-1",
            category="Sports",
        )
        return await matcher.find_matches([polymarket], [kalshi])

    assert len(asyncio.run(find_pair())) == 1


def test_candidate_index_avoids_category_wide_all_to_all_comparisons():
    async def count_comparisons():
        matcher = MarketMatcher(min_similarity=0.90)
        original = matcher.calculate_similarity
        calls = 0

        def counted(left, right):
            nonlocal calls
            calls += 1
            return original(left, right)

        matcher.calculate_similarity = counted
        polymarket = [
            SimpleNamespace(
                active=True,
                question=f"Candidate alpha{index} elected?",
                market_id=f"poly-{index}",
                condition_id=f"condition-{index}",
                category="politics",
            )
            for index in range(50)
        ]
        kalshi = [
            SimpleNamespace(
                is_active=True,
                title=f"Candidate omega{index} elected?",
                matching_text=f"Candidate omega{index} elected?",
                ticker=f"kalshi-{index}",
                category="Politics",
            )
            for index in range(50)
        ]

        await matcher.find_matches(polymarket, kalshi)
        return calls

    assert asyncio.run(count_comparisons()) < 100


def test_subthreshold_candidates_are_reviewable_but_not_matched():
    async def find_candidates():
        matcher = MarketMatcher(min_similarity=0.90)
        polymarket = SimpleNamespace(
            active=True,
            question="Will Trump win the presidential election?",
            market_id="poly-1",
            condition_id="condition-1",
            category="politics",
        )
        kalshi = SimpleNamespace(
            is_active=True,
            title="Will Trump win the party nomination?",
            matching_text="Will Trump win the party nomination?",
            ticker="kalshi-1",
            category="Politics",
        )
        matches = await matcher.find_matches([polymarket], [kalshi])
        return matches, matcher.get_review_candidates()

    matches, candidates = asyncio.run(find_candidates())

    assert matches == []
    assert len(candidates) == 1
    assert 0.65 <= candidates[0].similarity_score < 0.90
