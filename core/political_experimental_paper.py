"""Capital-constrained, no-mutation political experimental paper accounting.

This focused domain boundary owns political paper semantics while the backing
tables remain in :mod:`utils.platform_opportunity_store`, allowing later book
evidence, signals, fills, and accounting events to share one SQLite transaction.
"""

from __future__ import annotations

from datetime import datetime

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
