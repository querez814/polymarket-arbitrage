"""Bounded, auditable allocation of semantic-verifier candidates.

Retrieval score remains the quality signal. This module only prevents a single
repetitive event family from monopolizing the fixed verification budget and
reserves a small, deterministic exploration lane for category/family coverage.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class DiscoveryCandidate:
    polymarket: Any
    kalshi: Any
    retrieval_score: float
    per_market_rank: int
    global_rank: int

    @property
    def pair_id(self) -> str:
        polymarket_id = self.polymarket.market_id
        kalshi_id = self.kalshi.market_id
        return f"poly:{polymarket_id}|kalshi:{kalshi_id}"

    @property
    def category(self) -> str:
        return str(self.polymarket.category)

    @property
    def event_family(self) -> str:
        left = str(getattr(self.polymarket, "event_family", "") or "")
        right = str(getattr(self.kalshi, "event_family", "") or "")
        return left if left == right or not right else f"{left}|{right}"


@dataclass(frozen=True)
class AllocationDecision:
    candidate: DiscoveryCandidate
    selected: bool
    lane: str
    selected_rank: int | None
    rejection_reason: str


@dataclass(frozen=True)
class AllocationResult:
    selected: tuple[DiscoveryCandidate, ...]
    verification_sample: tuple[DiscoveryCandidate, ...]
    decisions: tuple[AllocationDecision, ...]
    metrics: dict[str, Any]


class StratifiedVerifierAllocator:
    """Allocate a fixed budget with family caps and deterministic exploration."""

    def __init__(
        self,
        *,
        category_cap_share: float = 0.60,
        family_cap_share: float = 0.40,
        exploration_share: float = 0.10,
    ):
        if not 0 < category_cap_share <= 1:
            raise ValueError("category_cap_share must be in (0, 1]")
        if not 0 < family_cap_share <= 1:
            raise ValueError("family_cap_share must be in (0, 1]")
        if not 0 <= exploration_share < 1:
            raise ValueError("exploration_share must be in [0, 1)")
        self.category_cap_share = category_cap_share
        self.family_cap_share = family_cap_share
        self.exploration_share = exploration_share

    def allocate(
        self,
        candidates: Sequence[DiscoveryCandidate],
        *,
        limit: int,
    ) -> AllocationResult:
        if limit <= 0:
            raise ValueError("verification limit must be positive")
        ranked = sorted(
            candidates,
            key=lambda item: (-item.retrieval_score, item.global_rank, item.pair_id),
        )
        budget = min(limit, len(ranked))
        baseline = ranked[:budget]
        family_cap = max(1, math.floor(limit * self.family_cap_share))
        category_cap = max(1, math.floor(limit * self.category_cap_share))
        exploration_budget = min(
            budget,
            (
                max(1, math.floor(limit * self.exploration_share))
                if self.exploration_share
                else 0
            ),
        )

        selected: list[DiscoveryCandidate] = []
        selected_ids: set[str] = set()
        family_counts: Counter[str] = Counter()
        category_counts: Counter[str] = Counter()
        lanes: dict[str, str] = {}

        family_queues: dict[tuple[str, str], deque[DiscoveryCandidate]] = {}
        for candidate in ranked:
            family_queues.setdefault(
                (candidate.category, candidate.event_family), deque()
            ).append(candidate)
        strata = sorted(
            family_queues,
            key=lambda key: (
                -family_queues[key][0].retrieval_score,
                key[0],
                key[1],
            ),
        )
        while len(selected) < exploration_budget and strata:
            progressed = False
            for stratum in strata:
                queue = family_queues[stratum]
                if not queue or len(selected) >= exploration_budget:
                    continue
                candidate = queue.popleft()
                self._select(
                    candidate,
                    lane="exploration",
                    selected=selected,
                    selected_ids=selected_ids,
                    family_counts=family_counts,
                    category_counts=category_counts,
                    lanes=lanes,
                )
                progressed = True
            if not progressed:
                break

        for candidate in ranked:
            if len(selected) >= budget:
                break
            if candidate.pair_id in selected_ids:
                continue
            if family_counts[candidate.event_family] >= family_cap:
                continue
            if category_counts[candidate.category] >= category_cap:
                continue
            self._select(
                candidate,
                lane="stratified",
                selected=selected,
                selected_ids=selected_ids,
                family_counts=family_counts,
                category_counts=category_counts,
                lanes=lanes,
            )

        flowback_selected = 0
        for candidate in ranked:
            if len(selected) >= budget:
                break
            if candidate.pair_id in selected_ids:
                continue
            self._select(
                candidate,
                lane="global_flowback",
                selected=selected,
                selected_ids=selected_ids,
                family_counts=family_counts,
                category_counts=category_counts,
                lanes=lanes,
            )
            flowback_selected += 1

        selected_rank = {
            candidate.pair_id: index
            for index, candidate in enumerate(selected, start=1)
        }
        baseline_ids = {candidate.pair_id for candidate in baseline}
        decisions = tuple(
            AllocationDecision(
                candidate=candidate,
                selected=candidate.pair_id in selected_ids,
                lane=lanes.get(candidate.pair_id, "not_selected"),
                selected_rank=selected_rank.get(candidate.pair_id),
                rejection_reason=(
                    "verification_budget_exhausted"
                    if candidate.pair_id not in selected_ids
                    else ""
                ),
            )
            for candidate in ranked
        )
        selected_categories = Counter(item.category for item in selected)
        selected_families = Counter(item.event_family for item in selected)
        verification_sample = _shadow_union_sample(
            baseline=baseline,
            stratified=selected,
            limit=budget,
        )
        sample_ids = {item.pair_id for item in verification_sample}
        return AllocationResult(
            selected=tuple(selected),
            verification_sample=tuple(verification_sample),
            decisions=decisions,
            metrics={
                "allocation_strategy": "stratified_v1",
                "verification_budget": limit,
                "family_cap": family_cap,
                "category_cap": category_cap,
                "exploration_budget": exploration_budget,
                "flowback_selected": flowback_selected,
                "baseline_category_counts": dict(
                    sorted(Counter(item.category for item in baseline).items())
                ),
                "baseline_family_counts": dict(
                    sorted(Counter(item.event_family for item in baseline).items())
                ),
                "stratified_category_counts": dict(sorted(selected_categories.items())),
                "stratified_family_counts": dict(sorted(selected_families.items())),
                "selected_category_entropy": _entropy(selected_categories),
                "selection_overlap": len(baseline_ids & selected_ids),
                "verification_sample_strategy": "shadow_union_v1",
                "verification_sample_baseline": len(sample_ids & baseline_ids),
                "verification_sample_stratified": len(sample_ids & selected_ids),
            },
        )

    @staticmethod
    def _select(
        candidate: DiscoveryCandidate,
        *,
        lane: str,
        selected: list[DiscoveryCandidate],
        selected_ids: set[str],
        family_counts: Counter[str],
        category_counts: Counter[str],
        lanes: dict[str, str],
    ) -> None:
        selected.append(candidate)
        selected_ids.add(candidate.pair_id)
        family_counts[candidate.event_family] += 1
        category_counts[candidate.category] += 1
        lanes[candidate.pair_id] = lane


def _entropy(counts: Counter[str]) -> float:
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return -sum(
        (count / total) * math.log2(count / total) for count in counts.values() if count
    )


def _shadow_union_sample(
    *,
    baseline: Sequence[DiscoveryCandidate],
    stratified: Sequence[DiscoveryCandidate],
    limit: int,
) -> list[DiscoveryCandidate]:
    baseline_ids = {item.pair_id for item in baseline}
    stratified_ids = {item.pair_id for item in stratified}
    by_id = {item.pair_id: item for item in (*baseline, *stratified)}
    shared = sorted(
        baseline_ids & stratified_ids,
        key=lambda pair_id: by_id[pair_id].global_rank,
    )
    baseline_only = sorted(
        baseline_ids - stratified_ids,
        key=lambda pair_id: by_id[pair_id].global_rank,
    )
    stratified_only = sorted(
        stratified_ids - baseline_ids,
        key=lambda pair_id: by_id[pair_id].global_rank,
    )
    chosen = shared[:limit]
    while len(chosen) < limit and (baseline_only or stratified_only):
        if baseline_only and len(chosen) < limit:
            chosen.append(baseline_only.pop(0))
        if stratified_only and len(chosen) < limit:
            chosen.append(stratified_only.pop(0))
    return [by_id[pair_id] for pair_id in chosen]
