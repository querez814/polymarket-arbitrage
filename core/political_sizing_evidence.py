"""Read-only reconstruction of sealed counterfactual-sizing evidence.

The sizing evaluator intentionally has no database dependency.  This module is
the narrow bridge from the durable control-paper/replay graph to that pure
boundary: it reconstructs only actual causal fills and exits, never scores,
allocates, or writes a scenario row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from core.political_sizing_report import (
    PoliticalSizingDepthLevel,
    PoliticalSizingExitEvidence,
    PoliticalSizingOpportunity,
)
from utils.platform_opportunity_store import PlatformOpportunityStore


@dataclass(frozen=True)
class PoliticalSizingLifecycleEvidence:
    """One immutable lifecycle fact shared by every sizing interpretation.

    This is intentionally broader than an allocatable opportunity.  A
    no-signal, pending signal, terminal no-fill, or no-exit is evidence that
    every counterfactual must see, even though none may become hypothetical
    PnL.  ``replay_hash`` always names the sealed replay token on which the
    fact was decided; ``signal_id`` is absent only for a sealed no-signal.
    """

    evidence_cohort_id: str
    kind: Literal["signal", "no_signal", "pending", "filled", "no_fill", "exit"]
    replay_sequence: int
    replay_hash: str
    contract_id: str
    signal_id: str | None = None
    outcome: str | None = None
    reason: str | None = None
    trigger: str | None = None

    def __post_init__(self) -> None:
        if (
            not all(
                (
                    self.evidence_cohort_id,
                    self.replay_hash,
                    self.contract_id,
                )
            )
            or self.replay_sequence < 1
        ):
            raise ValueError("sealed lifecycle evidence identity is required")
        if self.kind == "no_signal":
            if self.signal_id is not None:
                raise ValueError("sealed no-signal evidence cannot have a signal id")
        elif not self.signal_id:
            raise ValueError("sealed lifecycle evidence requires a signal id")


def sealed_political_sizing_lifecycle_evidence(
    *, store: PlatformOpportunityStore, cohort_id: str
) -> tuple[PoliticalSizingLifecycleEvidence, ...]:
    """Load the complete sealed lifecycle stream without allocating or writing.

    The legacy entry/exit projection below deliberately remains narrow for the
    existing evaluator.  This universal stream is the common counterfactual
    input: it retains scored no-signals, pending state, terminal no-fills, and
    all exit attempts (including ``no_exit``) so a scenario cannot manufacture
    a missing trade or favorable exit by observing only control allocations.
    """
    if not cohort_id.strip():
        raise ValueError("evidence cohort id must be non-empty")
    with store._lock:
        scored_rows = store._connection.execute(
            "SELECT decision.replay_sequence, replay.state_hash, replay.contract_id, "
            "decision.signal_id, decision.outcome "
            "FROM political_scored_decisions AS decision "
            "JOIN platform_replay_observation_events AS replay "
            "ON replay.cohort_id = decision.cohort_id "
            "AND replay.sequence = decision.replay_sequence "
            "WHERE decision.cohort_id = ? "
            "ORDER BY decision.replay_sequence, decision.model_version",
            (cohort_id,),
        ).fetchall()
        pending_rows = store._connection.execute(
            "SELECT pending.replay_sequence, replay.state_hash, pending.contract_id, "
            "pending.signal_id FROM political_experimental_pending_signals AS pending "
            "JOIN platform_replay_observation_events AS replay "
            "ON replay.cohort_id = pending.cohort_id "
            "AND replay.sequence = pending.replay_sequence "
            "WHERE pending.cohort_id = ? "
            "ORDER BY pending.replay_sequence, pending.signal_id",
            (cohort_id,),
        ).fetchall()
        fill_rows = store._connection.execute(
            "SELECT attempt.replay_sequence, replay.state_hash, pending.contract_id, "
            "attempt.signal_id, attempt.outcome, attempt.reason "
            "FROM political_experimental_fill_attempts AS attempt "
            "JOIN political_experimental_pending_signals AS pending "
            "ON pending.signal_id = attempt.signal_id "
            "JOIN platform_replay_observation_events AS replay "
            "ON replay.cohort_id = attempt.cohort_id "
            "AND replay.sequence = attempt.replay_sequence "
            "WHERE attempt.cohort_id = ? "
            "ORDER BY attempt.replay_sequence, attempt.signal_id",
            (cohort_id,),
        ).fetchall()
        exit_rows = store._connection.execute(
            "SELECT exit.replay_sequence, replay.state_hash, pending.contract_id, "
            "pending.signal_id, exit.outcome, exit.reason, exit.payload_json "
            "FROM political_experimental_pending_signals AS pending "
            "JOIN political_experimental_fill_attempts AS fill "
            "ON fill.signal_id = pending.signal_id AND fill.outcome = 'filled' "
            "JOIN political_experimental_exit_attempts AS exit "
            "ON exit.cohort_id = pending.cohort_id "
            "AND exit.position_id = 'position:' || pending.signal_id "
            "JOIN platform_replay_observation_events AS replay "
            "ON replay.cohort_id = exit.cohort_id "
            "AND replay.sequence = exit.replay_sequence "
            "WHERE pending.cohort_id = ? "
            "ORDER BY exit.replay_sequence, exit.position_id",
            (cohort_id,),
        ).fetchall()

    lifecycle: list[PoliticalSizingLifecycleEvidence] = []
    lifecycle.extend(
        PoliticalSizingLifecycleEvidence(
            evidence_cohort_id=cohort_id,
            kind=("signal" if row["outcome"] == "signal" else "no_signal"),
            replay_sequence=int(row["replay_sequence"]),
            replay_hash=str(row["state_hash"]),
            contract_id=str(row["contract_id"]),
            signal_id=(None if row["signal_id"] is None else str(row["signal_id"])),
            outcome=str(row["outcome"]),
        )
        for row in scored_rows
    )
    lifecycle.extend(
        PoliticalSizingLifecycleEvidence(
            evidence_cohort_id=cohort_id,
            kind="pending",
            replay_sequence=int(row["replay_sequence"]),
            replay_hash=str(row["state_hash"]),
            contract_id=str(row["contract_id"]),
            signal_id=str(row["signal_id"]),
        )
        for row in pending_rows
    )
    lifecycle.extend(
        PoliticalSizingLifecycleEvidence(
            evidence_cohort_id=cohort_id,
            kind=("filled" if row["outcome"] == "filled" else "no_fill"),
            replay_sequence=int(row["replay_sequence"]),
            replay_hash=str(row["state_hash"]),
            contract_id=str(row["contract_id"]),
            signal_id=str(row["signal_id"]),
            outcome=str(row["outcome"]),
            reason=(None if row["reason"] is None else str(row["reason"])),
        )
        for row in fill_rows
    )
    for row in exit_rows:
        payload = json.loads(str(row["payload_json"]))
        economics = payload.get("economics")
        trigger = economics.get("exit_trigger") if isinstance(economics, dict) else None
        lifecycle.append(
            PoliticalSizingLifecycleEvidence(
                evidence_cohort_id=cohort_id,
                kind="exit",
                replay_sequence=int(row["replay_sequence"]),
                replay_hash=str(row["state_hash"]),
                contract_id=str(row["contract_id"]),
                signal_id=str(row["signal_id"]),
                outcome=str(row["outcome"]),
                reason=(None if row["reason"] is None else str(row["reason"])),
                trigger=trigger if isinstance(trigger, str) else None,
            )
        )
    return tuple(
        sorted(
            lifecycle,
            key=lambda item: (
                item.replay_sequence,
                item.kind,
                item.signal_id or "",
            ),
        )
    )


def sealed_political_sizing_evidence(
    *, store: PlatformOpportunityStore, cohort_id: str
) -> tuple[
    tuple[PoliticalSizingOpportunity, ...], tuple[PoliticalSizingExitEvidence, ...]
]:
    """Load common evidence from durable control fills and triggered exits.

    A no-fill has no entry execution evidence and therefore cannot become a
    hypothetical allocation.  Likewise an exit without a sealed trigger is
    deliberately excluded rather than inventing a reason for a manual fixture
    or an incomplete historical row.  The returned values are chronological
    and all prices, depth, fee terms, and replay hashes come from the original
    persisted replay payloads.
    """
    if not cohort_id.strip():
        raise ValueError("evidence cohort id must be non-empty")
    with store._lock:  # Read one durable graph snapshot; this performs no writes.
        rows = store._connection.execute(
            "SELECT p.signal_id, p.event_id, p.milestone_id, p.contract_id, "
            "p.base_lane, p.side, f.replay_sequence, replay.state_hash, replay.fee_hash "
            "FROM political_experimental_pending_signals AS p "
            "JOIN political_experimental_fill_attempts AS f "
            "ON f.signal_id = p.signal_id "
            "JOIN platform_replay_observation_events AS replay "
            "ON replay.cohort_id = f.cohort_id AND replay.sequence = f.replay_sequence "
            "WHERE p.cohort_id = ? AND f.outcome = 'filled' "
            "ORDER BY f.replay_sequence, p.signal_id",
            (cohort_id,),
        ).fetchall()
        exit_rows = store._connection.execute(
            "SELECT p.contract_id, p.side, exit.replay_sequence, replay.state_hash, replay.fee_hash, "
            "exit.payload_json "
            "FROM political_experimental_pending_signals AS p "
            "JOIN political_experimental_fill_attempts AS fill "
            "ON fill.signal_id = p.signal_id AND fill.outcome = 'filled' "
            "JOIN political_experimental_exit_attempts AS exit "
            "ON exit.cohort_id = p.cohort_id "
            "AND exit.position_id = 'position:' || p.signal_id "
            "JOIN platform_replay_observation_events AS replay "
            "ON replay.cohort_id = exit.cohort_id AND replay.sequence = exit.replay_sequence "
            "WHERE p.cohort_id = ? AND exit.outcome IN ('partial', 'closed') "
            "ORDER BY exit.replay_sequence, exit.position_id",
            (cohort_id,),
        ).fetchall()

    opportunities: list[PoliticalSizingOpportunity] = []
    for row in rows:
        book = store.replay_book_state(str(row["state_hash"]))
        fee = store.replay_fee_schedule(str(row["fee_hash"]))
        side = str(row["side"])
        opportunities.append(
            PoliticalSizingOpportunity(
                evidence_cohort_id=cohort_id,
                signal_id=str(row["signal_id"]),
                entry_replay_sequence=int(row["replay_sequence"]),
                entry_replay_hash=str(row["state_hash"]),
                event_id=str(row["event_id"]),
                milestone_id=str(row["milestone_id"]),
                contract_id=str(row["contract_id"]),
                base_lane=str(row["base_lane"]),
                side=side,
                fee_schedule=fee,
                levels=_levels(book, side=side, book_side="asks"),
            )
        )

    exits: list[PoliticalSizingExitEvidence] = []
    for row in exit_rows:
        payload = json.loads(str(row["payload_json"]))
        economics = payload.get("economics")
        trigger = economics.get("exit_trigger") if isinstance(economics, dict) else None
        # Older/manual rows cannot truthfully participate: no trigger is not
        # permission to choose a favorable counterfactual exit reason.
        if not isinstance(trigger, str) or not trigger:
            continue
        book = store.replay_book_state(str(row["state_hash"]))
        fee = store.replay_fee_schedule(str(row["fee_hash"]))
        exits.append(
            PoliticalSizingExitEvidence(
                evidence_cohort_id=cohort_id,
                contract_id=str(row["contract_id"]),
                exit_replay_sequence=int(row["replay_sequence"]),
                exit_replay_hash=str(row["state_hash"]),
                trigger=trigger,
                fee_schedule=fee,
                levels=_levels(book, side=str(row["side"]), book_side="bids"),
            )
        )
    return tuple(opportunities), tuple(exits)


def _levels(
    book: dict[str, object], *, side: str, book_side: str
) -> tuple[PoliticalSizingDepthLevel, ...]:
    token = book.get(side)
    if not isinstance(token, dict):
        raise ValueError("sealed replay book is missing the contract side")
    raw_levels = token.get(book_side)
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError("sealed replay book is missing executable depth")
    levels: list[PoliticalSizingDepthLevel] = []
    for level in raw_levels:
        if not isinstance(level, list) or len(level) != 2:
            raise ValueError("sealed replay book level is invalid")
        levels.append(
            PoliticalSizingDepthLevel(
                displayed_ask=str(level[0]), displayed_size=Decimal(str(level[1]))
            )
        )
    return tuple(levels)
