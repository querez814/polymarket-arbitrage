import random

import pytest

from utils.http_resilience import CircuitOpenError, EndpointResilience


def test_backoff_is_exponential_with_bounded_jitter():
    resilience = EndpointResilience(base_delay=1.0, random_source=random.Random(7))

    delays = [resilience.retry_delay(attempt) for attempt in range(3)]

    assert 1.0 <= delays[0] < 2.0
    assert 2.0 <= delays[1] < 3.0
    assert 4.0 <= delays[2] < 5.0


def test_circuit_opens_after_rolling_failure_threshold_and_recovers():
    now = 100.0
    resilience = EndpointResilience(
        window_size=5,
        minimum_calls=5,
        failure_threshold=0.6,
        cooldown_seconds=10,
        clock=lambda: now,
    )
    for succeeded in (True, False, False, False, True):
        resilience.record(succeeded=succeeded)

    with pytest.raises(CircuitOpenError):
        resilience.before_request()
    now += 11
    resilience.before_request()
    resilience.record(succeeded=True)

    assert resilience.metrics["circuit_open"] is False
    assert resilience.metrics["requests"] == 6


def test_retry_after_header_takes_precedence_over_computed_delay():
    resilience = EndpointResilience(base_delay=1.0)

    assert resilience.retry_delay(0, retry_after="7") == 7.0
