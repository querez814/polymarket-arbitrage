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
class PoliticalSizingExitEvidence:
    """Sealed exit evidence shared verbatim by every sizing scenario.

    It deliberately contains the canonical replay identity and the frozen
    trigger.  Scenario policies may change quantity, but never exit timing,
    trigger, fee evidence, or displayed depth.
    """

    evidence_cohort_id: str
    contract_id: str
    exit_replay_sequence: int
    exit_replay_hash: str
    trigger: str
    fee_schedule: Mapping[str, Any]
    levels: tuple[PoliticalSizingDepthLevel, ...]

    def __post_init__(self) -> None:
        if not all(
            value
            for value in (
                self.evidence_cohort_id,
                self.contract_id,
                self.exit_replay_hash,
                self.trigger,
            )
        ):
            raise ValueError("sealed political sizing exit identity is required")
        if self.exit_replay_sequence < 1:
            raise ValueError("political sizing exit replay sequence must be positive")
        if not self.levels:
            raise ValueError("political sizing exit requires persisted levels")


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
class PoliticalSizingExit:
    """One hypothetical exit, calculated only from sealed exit evidence."""

    contract_id: str
    exit_replay_sequence: int
    exit_replay_hash: str
    trigger: str
    requested_quantity: int
    executable_quantity: int
    credit_micros: int
    released_basis_micros: int
    realized_pnl_micros: int | None
    remaining_quantity: int
    saturation_reason: str | None


@dataclass(frozen=True)
class PoliticalSizingScenarioReport:
    """Chronological read-only scenario outcome with no account side effects."""

    scenario_id: str
    scenario_name: str
    evidence_cohort_id: str
    capital_used_micros: int
    capital_rejected_micros: int
    unused_eligible_quantity: int
    peak_capital_used_micros: int
    capital_utilization_ratio: Decimal | None
    allocations: tuple[PoliticalSizingAllocation, ...]
    exits: tuple[PoliticalSizingExit, ...]
    realized_pnl_micros: int | None
    open_unrealized_pnl_micros: int | None
    open_valuation_complete: bool
    maximum_drawdown_micros: int | None


def evaluate_political_sizing_scenario(
    *,
    scenario: PoliticalSizingScenario,
    opportunities: Sequence[PoliticalSizingOpportunity],
    exit_evidence: Sequence[PoliticalSizingExitEvidence] = (),
    starting_cash_micros: int,
    displayed_depth_fraction: Decimal = Decimal("0.10"),
) -> PoliticalSizingScenarioReport:
    """Allocate immutable entry evidence chronologically without mutations.

    Entry and exit evidence are consumed in canonical replay order.  The
    evaluator is intentionally store-free: it releases hypothetical reserve
    and reports PnL, but cannot create a control-ledger row or venue request.
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
    if any(item.evidence_cohort_id != cohort_id for item in exit_evidence):
        raise ValueError("a scenario cannot combine evidence cohorts")

    scenario_id = scenario.scenario_id(evidence_cohort_id=cohort_id)
    reserved_micros = 0
    open_contracts: set[str] = set()
    open_occurrences: set[tuple[str, str]] = set()
    open_positions: dict[str, dict[str, int | str]] = {}
    allocations: list[PoliticalSizingAllocation] = []
    exits: list[PoliticalSizingExit] = []
    realized_pnl_micros = 0
    peak_capital_used_micros = 0
    capital_rejected_micros = 0
    unused_eligible_quantity = 0
    peak_realized_pnl_micros = 0
    maximum_drawdown_micros = 0
    completed_exit = False
    timeline = sorted(
        (
            *((item.entry_replay_sequence, "entry", item) for item in opportunities),
            *((item.exit_replay_sequence, "exit", item) for item in exit_evidence),
        ),
        key=lambda item: (item[0], item[1]),
    )
    for _, kind, evidence in timeline:
        if kind == "exit":
            assert isinstance(evidence, PoliticalSizingExitEvidence)
            position = open_positions.get(evidence.contract_id)
            requested = sum(
                int(level.displayed_size * displayed_depth_fraction)
                for level in evidence.levels
            )
            if position is None:
                exits.append(
                    PoliticalSizingExit(
                        contract_id=evidence.contract_id,
                        exit_replay_sequence=evidence.exit_replay_sequence,
                        exit_replay_hash=evidence.exit_replay_hash,
                        trigger=evidence.trigger,
                        requested_quantity=requested,
                        executable_quantity=0,
                        credit_micros=0,
                        released_basis_micros=0,
                        realized_pnl_micros=None,
                        remaining_quantity=0,
                        saturation_reason="no_open_position",
                    )
                )
                continue
            remaining_quantity = int(position["quantity"])
            level_economics = []
            for level in evidence.levels:
                eligible = min(
                    int(level.displayed_size * displayed_depth_fraction),
                    remaining_quantity,
                )
                if eligible <= 0:
                    continue
                economics = PoliticalExperimentalPaperLedger.exit_economics(
                    quantity=eligible,
                    displayed_bid=level.displayed_ask,
                    fee_schedule=dict(evidence.fee_schedule),
                )
                level_economics.append((level.displayed_ask, economics))
                remaining_quantity -= eligible
                if remaining_quantity == 0:
                    break
            executable = sum(item.quantity for _, item in level_economics)
            if executable:
                _, balance_change = (
                    PoliticalExperimentalPaperLedger._order_level_payloads(
                        levels=level_economics, direction="exit"
                    )
                )
                basis = int(position["basis_micros"])
                quantity = int(position["quantity"])
                released = basis * executable // quantity
                pnl = balance_change - released
                position["quantity"] = quantity - executable
                position["basis_micros"] = basis - released
                reserved_micros -= released
                realized_pnl_micros += pnl
                completed_exit = True
                peak_realized_pnl_micros = max(
                    peak_realized_pnl_micros, realized_pnl_micros
                )
                maximum_drawdown_micros = max(
                    maximum_drawdown_micros,
                    peak_realized_pnl_micros - realized_pnl_micros,
                )
                if int(position["quantity"]) == 0:
                    open_contracts.remove(evidence.contract_id)
                    open_occurrences.remove(
                        (str(position["milestone_id"]), str(position["base_lane"]))
                    )
                    del open_positions[evidence.contract_id]
                exits.append(
                    PoliticalSizingExit(
                        contract_id=evidence.contract_id,
                        exit_replay_sequence=evidence.exit_replay_sequence,
                        exit_replay_hash=evidence.exit_replay_hash,
                        trigger=evidence.trigger,
                        requested_quantity=requested,
                        executable_quantity=executable,
                        credit_micros=balance_change,
                        released_basis_micros=released,
                        realized_pnl_micros=pnl,
                        remaining_quantity=remaining_quantity,
                        saturation_reason=(
                            None
                            if executable == requested
                            else "insufficient_exit_depth"
                        ),
                    )
                )
            else:
                exits.append(
                    PoliticalSizingExit(
                        contract_id=evidence.contract_id,
                        exit_replay_sequence=evidence.exit_replay_sequence,
                        exit_replay_hash=evidence.exit_replay_hash,
                        trigger=evidence.trigger,
                        requested_quantity=requested,
                        executable_quantity=0,
                        credit_micros=0,
                        released_basis_micros=0,
                        realized_pnl_micros=None,
                        remaining_quantity=remaining_quantity,
                        saturation_reason="insufficient_exit_depth",
                    )
                )
            continue
        assert isinstance(evidence, PoliticalSizingOpportunity)
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
            peak_capital_used_micros = max(peak_capital_used_micros, reserved_micros)
            open_contracts.add(evidence.contract_id)
            open_occurrences.add(occurrence)
            open_positions[evidence.contract_id] = {
                "quantity": executable,
                "basis_micros": debit,
                "milestone_id": evidence.milestone_id,
                "base_lane": evidence.base_lane,
            }
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
    unused_eligible_quantity = sum(
        allocation.unused_eligible_quantity for allocation in allocations
    )
    capital_rejected_micros = sum(
        max(
            0,
            _requested_entry_debit(
                evidence=next(
                    item
                    for item in opportunities
                    if item.signal_id == allocation.signal_id
                ),
                displayed_depth_fraction=displayed_depth_fraction,
            )
            - allocation.capital_used_micros,
        )
        for allocation in allocations
    )
    utilization_cap = (
        starting_cash_micros
        if scenario.total_reserved_cap is None
        else min(
            starting_cash_micros,
            int(scenario.total_reserved_cap * Decimal("1000000")),
        )
    )
    return PoliticalSizingScenarioReport(
        scenario_id=scenario_id,
        scenario_name=scenario.name,
        evidence_cohort_id=cohort_id,
        capital_used_micros=reserved_micros,
        capital_rejected_micros=capital_rejected_micros,
        unused_eligible_quantity=unused_eligible_quantity,
        peak_capital_used_micros=peak_capital_used_micros,
        capital_utilization_ratio=(
            None
            if utilization_cap == 0
            else Decimal(peak_capital_used_micros) / Decimal(utilization_cap)
        ),
        allocations=tuple(allocations),
        exits=tuple(exits),
        realized_pnl_micros=(realized_pnl_micros if completed_exit else None),
        open_unrealized_pnl_micros=None,
        open_valuation_complete=not open_positions,
        maximum_drawdown_micros=(maximum_drawdown_micros if completed_exit else None),
    )


def _requested_entry_debit(
    *,
    evidence: PoliticalSizingOpportunity,
    displayed_depth_fraction: Decimal,
) -> int:
    """Quote all eligible persisted depth with the ledger's order rounding."""
    economics = []
    for level in evidence.levels:
        quantity = int(level.displayed_size * displayed_depth_fraction)
        if quantity > 0:
            economics.append(
                (
                    level.displayed_ask,
                    PoliticalExperimentalPaperLedger.entry_economics(
                        quantity=quantity,
                        displayed_ask=level.displayed_ask,
                        fee_schedule=dict(evidence.fee_schedule),
                    ),
                )
            )
    if not economics:
        return 0
    _, balance_change = PoliticalExperimentalPaperLedger._order_level_payloads(
        levels=economics, direction="entry"
    )
    return -balance_change


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
