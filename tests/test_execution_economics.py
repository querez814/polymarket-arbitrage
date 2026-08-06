from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.execution_economics import (
    AuthoritativeEconomicsProvider,
    EconomicsUnavailableError,
    PairEconomics,
)
from core.cross_platform_arb import MarketPair
from kalshi_client.models import KalshiFeeSchedule


def _snapshot(**overrides):
    values = {
        "pair_id": "poly:condition-1|kalshi:TICKER-1",
        "polymarket_market_id": "condition-1",
        "kalshi_ticker": "TICKER-1",
        "polymarket_fee_rate": Decimal("0.07"),
        "polymarket_fee_exponent": Decimal("1"),
        "polymarket_taker_only": True,
        "polymarket_order_gas_cost": Decimal("0"),
        "polymarket_gas_source": "offchain_clob_order",
        "kalshi_fee_type": "quadratic",
        "kalshi_fee_multiplier": Decimal("1"),
        "observed_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return PairEconomics(**values)


def test_authoritative_economics_uses_pair_metadata_and_conservative_rounding():
    snapshot = _snapshot()

    total = snapshot.total_taker_cost(
        token="YES",
        polymarket_price=Decimal("0.40"),
        kalshi_price=Decimal("0.65"),
        size=Decimal("10"),
    )

    assert total == Decimal("0.327300")


def test_kalshi_trade_fee_rounds_up_to_current_centicent_precision():
    snapshot = _snapshot()

    total = snapshot.total_taker_cost(
        token="YES",
        polymarket_price=Decimal("0.40"),
        kalshi_price=Decimal("0.055"),
        size=Decimal("1"),
    )

    # Current official Kalshi fee rounding ceilings the trade fee to $0.0001.
    assert total == Decimal("0.020500")


def test_authoritative_economics_fails_closed_when_stale_or_unsupported():
    stale = _snapshot(observed_at=datetime.now(timezone.utc) - timedelta(seconds=31))
    with pytest.raises(EconomicsUnavailableError, match="stale"):
        stale.require_fresh(max_age=timedelta(seconds=30))

    unsupported = _snapshot(kalshi_fee_type="flat")
    with pytest.raises(EconomicsUnavailableError, match="unsupported"):
        unsupported.total_taker_cost(
            token="YES",
            polymarket_price=Decimal("0.40"),
            kalshi_price=Decimal("0.65"),
            size=Decimal("10"),
        )


@pytest.mark.asyncio
async def test_provider_binds_current_public_fee_metadata_to_exact_pair():
    class Poly:
        async def get_clob_market_info(self, market_id):
            assert market_id == "condition-live-1"
            return {"tbf": 1000, "fd": {"r": 0.07, "e": 1, "to": True}}

    class Kalshi:
        async def get_fee_schedule(self, ticker):
            assert ticker == "TICKER-1"
            return KalshiFeeSchedule("quadratic", 1.0, "event")

    pair = MarketPair(
        polymarket_id="gamma-1",
        polymarket_question="Question?",
        kalshi_ticker="TICKER-1",
        kalshi_title="Question?",
        similarity_score=1.0,
        polymarket_condition_id="condition-live-1",
    )
    snapshot = await AuthoritativeEconomicsProvider(Poly(), Kalshi()).quote_pair(pair)

    assert snapshot.pair_id == pair.pair_id
    assert snapshot.polymarket_market_id == "condition-live-1"
    assert snapshot.polymarket_fee_rate == Decimal("0.07")
    assert snapshot.polymarket_fee_exponent == Decimal("1")
    assert snapshot.polymarket_taker_only is True
    assert snapshot.polymarket_order_gas_cost == Decimal("0")
    assert snapshot.polymarket_gas_source == "offchain_clob_order"
    assert snapshot.kalshi_fee_multiplier == Decimal("1.0")


@pytest.mark.asyncio
async def test_provider_accepts_explicitly_fee_free_polymarket_market():
    class Poly:
        async def get_clob_market_info(self, market_id):
            return {"tbf": 0, "fd": None}

    class Kalshi:
        async def get_fee_schedule(self, ticker):
            return KalshiFeeSchedule("quadratic", 1.0, "series")

    pair = MarketPair("condition-1", "Question?", "TICKER-1", "Question?", 1.0)
    snapshot = await AuthoritativeEconomicsProvider(Poly(), Kalshi()).quote_pair(pair)

    assert snapshot.polymarket_fee_rate == Decimal("0")
    assert snapshot.polymarket_fee_exponent == Decimal("1")


@pytest.mark.asyncio
async def test_provider_reuses_fresh_pair_economics_to_protect_venue_rate_limits():
    calls = {"polymarket": 0, "kalshi": 0}

    class Poly:
        async def get_clob_market_info(self, market_id):
            calls["polymarket"] += 1
            return {"tbf": 0, "fd": None}

    class Kalshi:
        async def get_fee_schedule(self, ticker):
            calls["kalshi"] += 1
            return KalshiFeeSchedule("quadratic", 1.0, "series")

    pair = MarketPair("condition-1", "TICKER-1", "Question?", "Question?", 1.0)
    provider = AuthoritativeEconomicsProvider(
        Poly(),
        Kalshi(),
        cache_ttl=timedelta(seconds=20),
    )

    first = await provider.quote_pair(pair)
    second = await provider.quote_pair(pair)

    assert second is first
    assert calls == {"polymarket": 1, "kalshi": 1}


@pytest.mark.asyncio
async def test_provider_turns_transport_failure_into_fail_closed_economics_error():
    class Poly:
        async def get_clob_market_info(self, market_id):
            raise OSError("temporary DNS failure")

    class Kalshi:
        async def get_fee_schedule(self, ticker):
            return KalshiFeeSchedule("quadratic", 1.0, "series")

    pair = MarketPair("condition-1", "TICKER-1", "Question?", "Question?", 1.0)

    with pytest.raises(EconomicsUnavailableError, match="unavailable") as failure:
        await AuthoritativeEconomicsProvider(Poly(), Kalshi()).quote_pair(pair)

    assert isinstance(failure.value.__cause__, OSError)
