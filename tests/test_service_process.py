import socket
import subprocess
import sys
import signal
from pathlib import Path

import pytest

from scripts.wait_for_health import wait_for_health

ROOT = Path(__file__).parents[1]


def _free_port() -> int:
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("sandbox does not permit loopback process probes")
        return int(listener.getsockname()[1])


def _start_server(port: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "dashboard.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_dashboard_process_survives_clean_sigterm_restart_cycle():
    port = _free_port()
    url = f"http://127.0.0.1:{port}/health/live"

    first = _start_server(port)
    try:
        assert wait_for_health(url, timeout_seconds=10)
    finally:
        first.terminate()
        first.wait(timeout=10)
    assert first.returncode in {0, -signal.SIGTERM}

    second = _start_server(port)
    try:
        assert wait_for_health(url, timeout_seconds=10)
    finally:
        second.terminate()
        second.wait(timeout=10)
    assert second.returncode in {0, -signal.SIGTERM}
