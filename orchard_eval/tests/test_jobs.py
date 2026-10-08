"""The job client sends a command once and waits on its id.

``orchard_env``'s ``exec`` held one POST open for the whole command and sent the
command again when that connection dropped. On one SWE-bench Pro V2 run that
started a second agent CLI in 297 of 613 trials, on the tree the first one had
already edited. These pin the replacement: exactly one POST per command, and
every failure after it either recovered by reading or raised.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import aiohttp
import pytest

from orchard_evalkit import jobs
from orchard_evalkit.jobs import (
    ExecDispatchError,
    JobClient,
    JobLostError,
    JobWaitError,
)


def _job(status, **fields):
    data = {
        "job_id": "j1",
        "sandbox_id": "s1",
        "command": "agent",
        "status": status,
        "created_at": 1.0,
    }
    data.update(fields)
    return data


DONE = _job("succeeded", exit_code=0, stdout="done\n", stderr="")
RUNNING = _job("running")


class _Answer:
    """An aiohttp response, as far as the client reads one."""

    def __init__(self, body=None, status=200):
        self.status = status
        self._body = body if body is not None else {"job_id": "j1"}

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                MagicMock(), (), status=self.status, message=str(self.status)
            )

    async def json(self):
        return self._body


class _Call:
    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Answers each request with the next outcome, or with *outcomes()*."""

    closed = False

    def __init__(self, outcomes):
        self._outcomes = outcomes
        self.calls = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if callable(self._outcomes):
            return _Call(self._outcomes())
        return _Call(self._outcomes.pop(0))

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(jobs, "POLL_MAX_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(jobs, "SUBMIT_BACKOFF_SEC", 0.0)
    monkeypatch.setattr(jobs, "RETRY_AFTER_503_SEC", 0.0)


def _client(submits=None, waits=None):
    client = JobClient("http://orchestrator/", api_key="k")
    client._submit_session = _Session([_Answer()] if submits is None else submits)
    client._wait_session = _Session([] if waits is None else waits)
    return client


def _run(client, **kwargs):
    return client.run("s1", "mini-swe-agent --task ...", timeout=3600, **kwargs)


class TestRun:
    @pytest.mark.asyncio
    async def test_a_command_is_submitted_once_and_waited_on_by_id(self):
        client = _client(waits=[_Answer(DONE)])
        result = await _run(client, cwd="/app", env={"A": "1"})

        assert result.succeeded
        assert result.stdout == "done\n"
        [(method, url, kwargs)] = client._submit_session.calls
        assert (method, url) == ("POST", "http://orchestrator/sandboxes/s1/exec")
        # The result no longer rides on the POST, so nothing holds it open.
        assert "wait" not in kwargs["json"]
        assert kwargs["json"]["cwd"] == "/app"
        assert kwargs["json"]["timeout_seconds"] == 3600
        [(method, url, kwargs)] = client._wait_session.calls
        assert (method, url) == ("GET", "http://orchestrator/jobs/j1/wait")
        assert kwargs["params"]["timeout"] <= jobs.WAIT_WINDOW_SEC

    @pytest.mark.asyncio
    async def test_a_dropped_wait_is_asked_again_and_the_command_is_not_resent(self):
        client = _client(
            waits=[
                aiohttp.ServerDisconnectedError(),
                _Answer(RUNNING),
                asyncio.TimeoutError(),
                _Answer(status=502),
                _Answer(DONE),
            ]
        )
        result = await _run(client)

        assert result.succeeded
        assert len(client._submit_session.calls) == 1
        assert len(client._wait_session.calls) == 5

    @pytest.mark.asyncio
    async def test_a_job_the_orchestrator_forgot_is_named(self):
        client = _client(waits=[_Answer(status=404)])
        with pytest.raises(JobLostError, match="j1"):
            await _run(client)

    @pytest.mark.asyncio
    async def test_a_client_error_from_the_wait_is_raised_as_it_is(self):
        client = _client(waits=[_Answer(status=400)])
        with pytest.raises(aiohttp.ClientResponseError):
            await _run(client)

    @pytest.mark.asyncio
    async def test_a_long_outage_gives_up_without_resending(self, monkeypatch):
        monkeypatch.setattr(jobs, "MAX_OUTAGE_SEC", 0.0)

        def dropped():
            return aiohttp.ServerDisconnectedError()

        client = _client(waits=dropped)
        with pytest.raises(JobWaitError, match="ServerDisconnectedError"):
            await _run(client)
        assert len(client._submit_session.calls) == 1


class TestSubmit:
    @pytest.mark.asyncio
    async def test_a_drop_after_sending_is_not_resent(self):
        client = _client(submits=[aiohttp.ServerDisconnectedError()])
        with pytest.raises(ExecDispatchError, match="not sent again"):
            await _run(client)
        assert len(client._submit_session.calls) == 1
        assert client._wait_session.calls == []

    @pytest.mark.asyncio
    async def test_a_timeout_after_sending_is_not_resent(self):
        client = _client(submits=[asyncio.TimeoutError()])
        with pytest.raises(ExecDispatchError):
            await _run(client)
        assert len(client._submit_session.calls) == 1

    @pytest.mark.asyncio
    async def test_a_connection_that_never_opened_is_retried(self):
        refused = aiohttp.ClientConnectorError(MagicMock(), OSError(111, "refused"))
        client = _client(submits=[refused, _Answer()], waits=[_Answer(DONE)])
        assert (await _run(client)).succeeded
        assert len(client._submit_session.calls) == 2

    @pytest.mark.asyncio
    async def test_a_503_was_not_accepted_and_is_retried(self):
        client = _client(
            submits=[_Answer(status=503), _Answer()], waits=[_Answer(DONE)]
        )
        assert (await _run(client)).succeeded
        assert len(client._submit_session.calls) == 2

    @pytest.mark.asyncio
    async def test_an_answer_from_the_orchestrator_is_raised_as_it_is(self):
        # A 404 is a sandbox the orchestrator no longer has, which the
        # environment reports as SandboxGone.
        client = _client(submits=[_Answer(status=404)])
        with pytest.raises(aiohttp.ClientResponseError) as caught:
            await _run(client)
        assert caught.value.status == 404
        assert len(client._submit_session.calls) == 1
