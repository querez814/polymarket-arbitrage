import pytest

from core.two_leg_execution import (
    ExecutionPhase,
    LegIntent,
    LegPhase,
    LegSide,
    TwoLegExecution,
)


def _execution(execution_id: str = "exec-42") -> TwoLegExecution:
    return TwoLegExecution(
        execution_id,
        LegIntent("poly", "polymarket", "condition-1", LegSide.BUY, 0.40, 10.0),
        LegIntent("kalshi", "kalshi", "ticker-1", LegSide.SELL, 0.65, 10.0),
    )


def test_stable_distinct_idempotency_keys_derive_from_immutable_intent():
    first = _execution()
    rebuilt = _execution()

    assert first.legs["poly"].idempotency_key == rebuilt.legs["poly"].idempotency_key
    assert (
        first.legs["kalshi"].idempotency_key == rebuilt.legs["kalshi"].idempotency_key
    )
    assert first.legs["poly"].idempotency_key != first.legs["kalshi"].idempotency_key
    assert (
        _execution("another-execution").legs["poly"].idempotency_key
        != first.legs["poly"].idempotency_key
    )


@pytest.mark.parametrize(
    "second, message",
    [
        (
            LegIntent("poly", "kalshi", "ticker-1", LegSide.SELL, 0.65, 10.0),
            "leg ids must be distinct",
        ),
        (
            LegIntent("kalshi", "Polymarket", "ticker-1", LegSide.SELL, 0.65, 10.0),
            "distinct venues",
        ),
        (
            LegIntent("kalshi", "kalshi", "ticker-1", LegSide.BUY, 0.65, 10.0),
            "opposing sides",
        ),
        (
            LegIntent("kalshi", "kalshi", "ticker-1", LegSide.SELL, 0.65, 9.0),
            "equal normalized sizes",
        ),
    ],
)
def test_invalid_hedge_plan_fails_closed(second: LegIntent, message: str):
    first = LegIntent("poly", "polymarket", "condition-1", LegSide.BUY, 0.40, 10.0)

    with pytest.raises(ValueError, match=message):
        TwoLegExecution("exec-42", first, second)


def test_partial_first_leg_fill_exposes_realized_and_potential_residual():
    execution = _execution()
    execution.start_submission("poly")
    execution.reconcile_leg(
        "poly",
        phase=LegPhase.OPEN,
        cumulative_filled_size=4.0,
        venue_order_id="poly-order-1",
    )

    assert execution.phase is ExecutionPhase.RESIDUAL_EXPOSURE
    assert execution.realized_residual_size == 4.0
    assert execution.potential_residual_range == (4.0, 10.0)

    execution.start_submission("kalshi")
    assert execution.potential_residual_range == (-6.0, 10.0)


def test_ambiguous_submission_requires_recovery_and_retains_full_risk_range():
    execution = _execution()
    execution.start_submission("poly")
    execution.mark_submission_ambiguous("poly")

    assert execution.phase is ExecutionPhase.RECOVERY_REQUIRED
    assert execution.potential_residual_range == (0.0, 10.0)

    execution.reconcile_leg(
        "poly",
        phase=LegPhase.CANCELLED,
        cumulative_filled_size=3.0,
        venue_order_id="poly-order-1",
    )
    assert execution.phase is ExecutionPhase.RESIDUAL_EXPOSURE
    assert execution.realized_residual_size == 3.0
    assert execution.potential_residual_range == (3.0, 3.0)


def test_balanced_terminal_partial_fills_are_complete_and_flat():
    execution = _execution()
    for leg_id, order_id in (("poly", "poly-order-1"), ("kalshi", "kalshi-order-1")):
        execution.start_submission(leg_id)
        execution.reconcile_leg(
            leg_id,
            phase=LegPhase.CANCELLED,
            cumulative_filled_size=4.0,
            venue_order_id=order_id,
        )

    assert execution.phase is ExecutionPhase.COMPLETE
    assert execution.realized_residual_size == 0.0
    assert execution.potential_residual_range == (0.0, 0.0)


def test_reconciliation_rejects_fill_regression_overfill_and_terminal_reopen():
    execution = _execution()
    execution.start_submission("poly")
    execution.reconcile_leg(
        "poly",
        phase=LegPhase.CANCELLED,
        cumulative_filled_size=4.0,
        venue_order_id="poly-order-1",
    )

    with pytest.raises(ValueError, match="cannot decrease"):
        execution.reconcile_leg(
            "poly",
            phase=LegPhase.CANCELLED,
            cumulative_filled_size=3.0,
            venue_order_id="poly-order-1",
        )
    with pytest.raises(ValueError, match="exceeds intended size"):
        execution.reconcile_leg(
            "poly",
            phase=LegPhase.CANCELLED,
            cumulative_filled_size=11.0,
            venue_order_id="poly-order-1",
        )
    with pytest.raises(ValueError, match="different phase"):
        execution.reconcile_leg(
            "poly",
            phase=LegPhase.OPEN,
            cumulative_filled_size=4.0,
            venue_order_id="poly-order-1",
        )
