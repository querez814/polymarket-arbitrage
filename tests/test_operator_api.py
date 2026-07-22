from dataclasses import dataclass

from fastapi.testclient import TestClient

from core.operations import OperatorAuthenticationError
from dashboard.server import app, configure_dashboard_runtime


@dataclass(frozen=True)
class Status:
    halted: bool
    reason: str
    source: str = "operator"
    updated_at: str = "2026-07-22T12:00:00Z"
    last_alert_error: str = ""


class Runtime:
    def __init__(self):
        self.panics: list[str] = []
        self.resumes: list[str] = []

    def operator_status(self, token: str):
        if token != "x" * 32:
            raise OperatorAuthenticationError("operator authentication failed")
        return Status(False, "operator approved")

    async def panic(self, token: str, *, reason: str):
        self.operator_status(token)
        self.panics.append(reason)
        return Status(True, reason)

    async def resume(self, token: str, *, reason: str):
        self.operator_status(token)
        self.resumes.append(reason)
        return Status(False, reason)

    def status(self):
        return {"ready": True}


def test_operator_routes_require_bearer_authentication_and_control_runtime():
    runtime = Runtime()
    configure_dashboard_runtime(runtime=runtime)
    client = TestClient(app)
    try:
        assert client.get("/api/operator/status").status_code == 401
        assert (
            client.get(
                "/api/operator/status",
                headers={"authorization": "Bearer wrong"},
            ).status_code
            == 401
        )

        headers = {"authorization": f"Bearer {'x' * 32}"}
        status = client.get("/api/operator/status", headers=headers)
        panic = client.post(
            "/api/operator/panic", headers=headers, json={"reason": "manual stop"}
        )
        resume = client.post(
            "/api/operator/resume", headers=headers, json={"reason": "reconciled"}
        )

        assert status.status_code == 200
        assert status.json()["operator"]["halted"] is False
        assert panic.status_code == 200
        assert resume.status_code == 200
        assert runtime.panics == ["manual stop"]
        assert runtime.resumes == ["reconciled"]
    finally:
        configure_dashboard_runtime(runtime=None)


def test_operator_routes_are_unavailable_without_owned_production_runtime():
    configure_dashboard_runtime(runtime=None)
    response = TestClient(app).get(
        "/api/operator/status",
        headers={"authorization": f"Bearer {'x' * 32}"},
    )
    assert response.status_code == 503
