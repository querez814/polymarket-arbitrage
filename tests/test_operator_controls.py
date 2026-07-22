from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.operations import (
    AdmissionLimitError,
    AlertDeliveryError,
    OperatorAuthenticationError,
    PersistentOperatorControls,
)


@dataclass
class RecordingAlerts:
    events: list[dict[str, str]] = field(default_factory=list)

    async def deliver(self, event: dict[str, str]) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_operator_control_is_halted_by_default_and_survives_restart(tmp_path):
    path = tmp_path / "operator.sqlite3"
    alerts = RecordingAlerts()
    controls = PersistentOperatorControls(path, auth_token="x" * 32, alert_sink=alerts)

    assert controls.status().halted is True
    with pytest.raises(OperatorAuthenticationError):
        await controls.resume("wrong-token", reason="unauthorized")

    await controls.resume("x" * 32, reason="offline recovery verified")
    assert controls.status().halted is False
    await controls.trip(reason="residual exposure", source="runtime")
    assert controls.status().halted is True
    controls.close()

    reopened = PersistentOperatorControls(path, auth_token="x" * 32)
    assert reopened.status().halted is True
    assert reopened.status().reason == "residual exposure"
    reopened.close()
    assert [event["kind"] for event in alerts.events] == ["operator_resumed", "panic"]


@pytest.mark.asyncio
async def test_operator_resume_fails_closed_when_external_alert_delivery_fails(tmp_path):
    class FailingAlerts:
        async def deliver(self, event: dict[str, str]) -> None:
            raise TimeoutError("alert endpoint unavailable")

    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3",
        auth_token="x" * 32,
        alert_sink=FailingAlerts(),
    )

    with pytest.raises(AlertDeliveryError, match="resume alert delivery failed"):
        await controls.resume("x" * 32, reason="operator approved")

    status = controls.status()
    controls.close()
    assert status.halted is True
    assert status.reason == "operator resume alert delivery failed"
    assert status.last_alert_error == "TimeoutError"


def test_authenticated_status_rejects_an_invalid_operator_token(tmp_path):
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token="x" * 32
    )
    try:
        with pytest.raises(OperatorAuthenticationError):
            controls.authenticated_status("wrong-token")
        assert controls.authenticated_status("x" * 32).halted is True
    finally:
        controls.close()


def test_order_attempt_budget_is_atomic_and_survives_restart(tmp_path):
    path = tmp_path / "operator.sqlite3"
    controls = PersistentOperatorControls(path, auth_token="x" * 32)
    controls.reserve_order_attempts(2, max_per_minute=2, max_per_day=10)
    controls.close()

    reopened = PersistentOperatorControls(path, auth_token="x" * 32)
    try:
        with pytest.raises(AdmissionLimitError, match="per-minute"):
            reopened.reserve_order_attempts(1, max_per_minute=2, max_per_day=10)
    finally:
        reopened.close()
