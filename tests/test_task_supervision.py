import httpx
import pytest

from utils.task_supervision import RestartingTaskSupervisor


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://trading-api.kalshi.com/events")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"upstream returned {status_code}",
        request=request,
        response=response,
    )


@pytest.mark.asyncio
async def test_transient_503_restarts_worker_with_exponential_backoff():
    attempts = 0
    delays = []
    retries = []
    running = True

    async def worker():
        nonlocal attempts, running
        attempts += 1
        if attempts == 1:
            raise _http_error(503)
        running = False

    async def fake_sleep(delay):
        delays.append(delay)

    supervisor = RestartingTaskSupervisor(
        "cross-platform discovery",
        base_delay=2.0,
        max_delay=30.0,
        sleep=fake_sleep,
        on_retry=lambda event: retries.append(event),
    )

    await supervisor.run(worker, should_run=lambda: running)

    assert attempts == 2
    assert delays == [2.0]
    assert retries[0].attempt == 1
    assert retries[0].error_type == "HTTPStatusError"


@pytest.mark.asyncio
async def test_authentication_failure_is_fatal_and_not_retried():
    attempts = 0

    async def worker():
        nonlocal attempts
        attempts += 1
        raise _http_error(401)

    supervisor = RestartingTaskSupervisor(
        "cross-platform discovery",
        sleep=lambda _delay: None,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await supervisor.run(worker, should_run=lambda: True)

    assert attempts == 1
