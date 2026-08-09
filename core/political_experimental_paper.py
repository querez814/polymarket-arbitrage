"""Capital-constrained, no-mutation political experimental paper accounting.

This focused domain boundary owns political paper semantics while the backing
tables remain in :mod:`utils.platform_opportunity_store`, allowing later book
evidence, signals, fills, and accounting events to share one SQLite transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from utils.platform_opportunity_store import PlatformOpportunityStore


class PoliticalExperimentalPaperLedger:
    """Initialize the durable experimental-paper account in integer micros.

    Fill/exit mechanics intentionally do not live here yet.  Keeping account
    initialization idempotent makes later worker restart recovery unable to
    reset capital or duplicate the opening accounting event.
    """

    def __init__(self, *, store: PlatformOpportunityStore, cohort_id: str) -> None:
        if not cohort_id.strip():
            raise ValueError("cohort_id must be non-empty")
        self.store = store
        self.cohort_id = cohort_id

    def initialize(
        self, *, starting_cash_micros: int, initialized_at: datetime
    ) -> dict[str, int | bool]:
        return self.store.initialize_political_experimental_paper_account(
            cohort_id=self.cohort_id,
            starting_cash_micros=starting_cash_micros,
            initialized_at=initialized_at,
        )

    def record_pending_signal(
        self,
        *,
        signal_id: str,
        replay_sequence: int,
        event_id: str,
        milestone_id: str,
        contract_id: str,
        side: str,
        base_lane: str,
        phase: str,
        signal_request_started_at: datetime,
        signal_received_at: datetime,
        expires_at: datetime,
        model_version: str,
        config_hash: str,
        state_hash: str,
        fee_hash: str,
        features: dict[str, Any],
    ) -> bool:
        """Durably hand a qualified reaction to later-book resolution only."""
        return self.store.record_political_experimental_pending_signal(
            signal_id=signal_id,
            cohort_id=self.cohort_id,
            replay_sequence=replay_sequence,
            event_id=event_id,
            milestone_id=milestone_id,
            contract_id=contract_id,
            side=side,
            base_lane=base_lane,
            phase=phase,
            signal_request_started_at=signal_request_started_at,
            signal_received_at=signal_received_at,
            expires_at=expires_at,
            model_version=model_version,
            config_hash=config_hash,
            state_hash=state_hash,
            fee_hash=fee_hash,
            features=features,
        )
