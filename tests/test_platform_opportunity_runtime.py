from datetime import datetime, timezone

import pytest

from core.platform_opportunities import VenueFeeSchedule
from core.platform_opportunity_runtime import PlatformOpportunityWorker
from polymarket_client.models import OrderBook


@pytest.mark.asyncio
async def test_worker_drains_queued_observations_before_shutdown():
    class System:
        def __init__(self):
            self.processed = []

        def set_fee_schedule(self, contract_id, schedule):
            pass

        def observe_book(self, contract_id, book, *, observed_at):
            self.processed.append(contract_id)

        def dashboard_summary(self):
            return {}

    system = System()
    worker = PlatformOpportunityWorker(system, queue_capacity=10)
    schedule = VenueFeeSchedule(
        "polymarket",
        "none",
        0,
        1,
        0,
        datetime.now(timezone.utc),
        "test",
    )
    await worker.start()
    for index in range(3):
        assert worker.submit_book(
            f"polymarket:{index}",
            OrderBook(market_id=str(index)),
            observed_at=datetime.now(timezone.utc),
            fee_schedule=schedule,
        )

    await worker.stop()

    assert system.processed == ["polymarket:0", "polymarket:1", "polymarket:2"]
    assert worker.processed == 3
