"""Read-only sizing interpretations of sealed political entry evidence.

This module intentionally has no store or adapter dependency.  It is the
first fan-out boundary for Gate 5: every scenario receives the same immutable
opportunity object and can only calculate a hypothetical entry allocation.
It cannot create a signal, replay observation, paper position, or venue call.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from core.political_experimental_paper import PoliticalExperimentalPaperLedger
from core.political_sizing_scenarios import PoliticalSizingScenario


@dataclass(frozen=True)
class PoliticalSizingDepthLevel:
    """One persisted entry level, before the shared 10% depth convention."""

    displayed_ask: str
    displayed_size: Decimal

    def __post_init__(self) -> None:
        if not self.displayed_size.is_finite() or self.displayed_size <= 0:
            raise ValueError("political sizing depth must be finite and positive")


@dataclass(frozen=True)
class PoliticalSizingOpportunity:
    """Sealed causal entry evidence shared verbatim by every scenario."""

    evidence_cohort_id: str
    signal_id: str
    entry_replay_sequence: int
    entry_replay_hash: str
    event_id: str
    milestone_id: str
    contract_id: str
    base_lane: str
    side: str
    fee_schedule: Mapping[str, Any]
    levels: tuple[PoliticalSizingDepthLevel, ...]

    def __post_init__(self) -> None:
        if not all(
            value
            for value in (
                self.evidence_cohort_id,
                self.signal_id,
                self.entry_replay_hash,
                self.event_id,
                self.milestone_id,
                self.contract_id,
                self.base_lane,
                self.side,
            )
        ):
            raise ValueError("sealed political sizing evidence identity is required")
        if self.entry_replay_sequence < 1:
            raise ValueError("political sizing replay sequence must be positive")
        if self.side not in {"yes", "no"}:
            raise ValueError("political sizing side must be yes or no")
        if not self.levels:
            raise ValueError("political sizing opportunity requires persisted levels")


@dataclass(frozen=True)
class PoliticalSizingAllocation:
    """One non-mutating scenario allocation or exact rejection."""

    scenario_id: str
    signal_id: str
    entry_replay_sequence: int
    entry_replay_hash: str
    requested_quantity: int
    executable_quantity: int
    capital_used_micros: int
    unused_eligible_quantity: int
    saturation_reason: str | None


@dataclass(frozen=True)
class PoliticalSizingScenarioReport:
    """Chronological read-only scenario outcome with no account side effects."""

    scenario_id: str
    scenario_name: str
    evidence_cohort_id: str
    capital_used_micros: int
    capital_rejected_micros: int
    allocations: tuple[PoliticalSizingAllocation, ...]


def evaluate_political_sizing_scenario(
    *,
    scenario: PoliticalSizingScenario,
    opportunities: Sequence[PoliticalSizingOpportunity],
    starting_cash_micros: int,
    displayed_depth_fraction: Decimal = Decimal("0.10"),
) -> PoliticalSizingScenarioReport:
    """Allocate immutable entry evidence chronologically without mutations.

    Positions stay hypothetically open in this entry-only slice.  Consequently
    the reserve and open-position caps remain consumed until the later
    exit-evidence fan-out is added; this is conservative and deterministic.
    """
    if starting_cash_micros < 0:
        raise ValueError("political sizing starting cash cannot be negative")
    if not displayed_depth_fraction.is_finite() or not (
        Decimal("0") < displayed_depth_fraction <= Decimal("1")
    ):
        raise ValueError("political sizing depth fraction must be in (0, 1]")
    if not opportunities:
        raise ValueError("political sizing requires sealed opportunities")
    cohort_id = opportunities[0].evidence_cohort_id
    if any(item.evidence_cohort_id != cohort_id for item in opportunities):
        raise ValueError("a scenario cannot combine evidence cohorts")

    scenario_id = scenario.scenario_id(evidence_cohort_id=cohort_id)
    reserved_micros = 0
    open_contracts: set[str] = set()
    open_occurrences: set[tuple[str, str]] = set()
    allocations: list[PoliticalSizingAllocation] = []
    for evidence in sorted(opportunities, key=lambda item: item.entry_replay_sequence):
        requested = sum(
            int(level.displayed_size * displayed_depth_fraction)
            for level in evidence.levels
        )
        if evidence.contract_id in open_contracts:
            allocations.append(
                _allocation(
                    scenario_id,
                    evidence,
                    requested,
                    0,
                    0,
                    requested,
                    "contract_overlap",
                )
            )
            continue
        occurrence = (evidence.milestone_id, evidence.base_lane)
        if occurrence in open_occurrences:
            allocations.append(
                _allocation(
                    scenario_id,
                    evidence,
                    requested,
                    0,
                    0,
                    requested,
                    "occurrence_overlap",
                )
            )
            continue
        if (
            scenario.max_open_positions is not None
            and len(open_contracts) >= scenario.max_open_positions
        ):
            allocations.append(
                _allocation(
                    scenario_id,
                    evidence,
                    requested,
                    0,
                    0,
                    requested,
                    "max_open_positions",
                )
            )
            continue
        position_cap = (
            None
            if scenario.position_cap is None
            else int(scenario.position_cap * Decimal("1000000"))
        )
        reserve_cap = (
            None
            if scenario.total_reserved_cap is None
            else int(scenario.total_reserved_cap * Decimal("1000000"))
        )
        remaining = min(
            starting_cash_micros - reserved_micros,
            position_cap if position_cap is not None else starting_cash_micros,
            (
                reserve_cap - reserved_micros
                if reserve_cap is not None
                else starting_cash_micros
            ),
        )
        level_economics = []
        for level in evidence.levels:
            eligible = int(level.displayed_size * displayed_depth_fraction)
            if eligible <= 0:
                continue
            quantity = _largest_affordable_quantity(
                eligible, level.displayed_ask, evidence.fee_schedule, remaining
            )
            if quantity <= 0:
                break
            economics = PoliticalExperimentalPaperLedger.entry_economics(
                quantity=quantity,
                displayed_ask=level.displayed_ask,
                fee_schedule=dict(evidence.fee_schedule),
            )
            level_economics.append((level.displayed_ask, economics))
            remaining -= economics.debit_micros
            if quantity < eligible:
                break
        if level_economics:
            level_payloads, balance_change = (
                PoliticalExperimentalPaperLedger._order_level_payloads(
                    levels=level_economics, direction="entry"
                )
            )
            del level_payloads  # Accounting is intentionally report-only in this slice.
            debit = -balance_change
            executable = sum(item.quantity for _, item in level_economics)
            reserved_micros += debit
            open_contracts.add(evidence.contract_id)
            open_occurrences.add(occurrence)
            allocations.append(
                _allocation(
                    scenario_id,
                    evidence,
                    requested,
                    executable,
                    debit,
                    requested - executable,
                    None if executable == requested else "capital_or_position_cap",
                )
            )
        else:
            allocations.append(
                _allocation(
                    scenario_id,
                    evidence,
                    requested,
                    0,
                    0,
                    requested,
                    "capital_or_position_cap",
                )
            )
    return PoliticalSizingScenarioReport(
        scenario_id=scenario_id,
        scenario_name=scenario.name,
        evidence_cohort_id=cohort_id,
        capital_used_micros=reserved_micros,
        capital_rejected_micros=0,
        allocations=tuple(allocations),
    )


def _largest_affordable_quantity(
    eligible: int, ask: str, fee_schedule: Mapping[str, Any], remaining: int
) -> int:
    low, high = 0, eligible
    while low < high:
        candidate = (low + high + 1) // 2
        economics = PoliticalExperimentalPaperLedger.entry_economics(
            quantity=candidate, displayed_ask=ask, fee_schedule=dict(fee_schedule)
        )
        if economics.debit_micros <= remaining:
            low = candidate
        else:
            high = candidate - 1
    return low


def _allocation(
    scenario_id: str,
    evidence: PoliticalSizingOpportunity,
    requested: int,
    executable: int,
    capital_used: int,
    unused: int,
    reason: str | None,
) -> PoliticalSizingAllocation:
    return PoliticalSizingAllocation(
        scenario_id=scenario_id,
        signal_id=evidence.signal_id,
        entry_replay_sequence=evidence.entry_replay_sequence,
        entry_replay_hash=evidence.entry_replay_hash,
        requested_quantity=requested,
        executable_quantity=executable,
        capital_used_micros=capital_used,
        unused_eligible_quantity=unused,
        saturation_reason=reason,
    )
