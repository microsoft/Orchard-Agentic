"""Run a command in an Orchard sandbox exactly once.

``orchard_env``'s ``AsyncSandboxInstance.exec`` carries a command and its result
on one ``POST /sandboxes/{id}/exec`` that is held open until the command ends.
When that connection drops, the SDK's retry loop and its fallback both POST the
command again — a new job, in the same pod. For an agent CLI that is a second
agent starting on the tree the first one already edited, and on one SWE-bench
Pro V2 run it happened in 297 of 613 trials. The SDK's own retry log goes to
stdout, so ``job.log`` never showed it.

The SDK is maintained elsewhere, so the fix lives here, and both ways this
repository runs an agent use it: ``EvalSandbox.exec`` for ``orchard-eval run``
and ``OrchardEnvironment.exec`` in ``harbor_orchard``. :class:`JobClient` uses
two routes the SDK already relies on, against the same orchestrator:

``POST /sandboxes/{id}/exec`` without ``wait``
    Answered as soon as the job exists. Sent once, on a connection of its own,
    and re-sent only when it provably never left this host.
``GET /jobs/{id}/wait``
    Long-polled in short windows. It only reads, so it is asked again as often
    as the connection needs, and no request is ever held open for a whole
    rollout.

What cannot be settled — a send that may or may not have reached the
orchestrator, a job it no longer knows, an orchestrator gone for minutes — is
raised as a :class:`JobTransportError` rather than guessed at.
"""

from __future__ import annotations

import asyncio
import random

import aiohttp
from orchard_env import JobResult

#: Seconds one ``GET /jobs/{id}/wait`` may hold its connection. Short on
#: purpose: a request held open for a whole agent rollout is what a load
#: balancer or proxy between here and the orchestrator drops.
WAIT_WINDOW_SEC = 30

#: Seconds the orchestrator may stay unreachable while a job runs before the
#: wait gives up on it. The job itself is not touched either way.
MAX_OUTAGE_SEC = 300

#: Longest pause between two polls that came back at once. The orchestrator
#: runs several replicas and only the one running the job can hold the wait
#: open; any other answers immediately with the job still running.
POLL_MAX_INTERVAL_SEC = 2.0

#: Bounds on the submit, which returns as soon as the job exists.
SUBMIT_TIMEOUT_SEC = 120
CONNECT_TIMEOUT_SEC = 30
SUBMIT_ATTEMPTS = 3

#: Pause before re-sending a submit that never left (doubling), and after a 503.
SUBMIT_BACKOFF_SEC = 1.0
RETRY_AFTER_503_SEC = 5.0

#: aiohttp's connect-phase timeout, where the installed version has one. A read
#: timeout after the request went out is a different thing and is not in here.
_CONNECT_TIMEOUTS = tuple(
    cls for cls in (getattr(aiohttp, "ConnectionTimeoutError", None),) if cls is not None
)


class JobTransportError(RuntimeError):
    """The orchestrator could not be reached. Says nothing about the command."""


class ExecDispatchError(JobTransportError):
    """``POST /exec`` failed after the orchestrator may already have accepted it.

    The command may be running in the sandbox or may never have started, and
    nothing on the API can tell which. It is deliberately not sent again.
    """


class JobLostError(JobTransportError):
    """The orchestrator has no record of a job this client submitted."""


class JobWaitError(JobTransportError):
    """The orchestrator stayed unreachable while a submitted job was running."""


class JobClient:
    """Submit-once, wait-by-id access to one orchestrator."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._submit_session: aiohttp.ClientSession | None = None
        self._wait_session: aiohttp.ClientSession | None = None

    async def run(
        self,
        sandbox_id: str,
        command: str,
        *,
        timeout: int,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = False,
    ) -> JobResult:
        """Run *command* once and return its result, as ``instance.exec`` does."""
        job_id = await self.submit(
            sandbox_id,
            command,
            timeout=timeout,
            cwd=cwd,
            env=env,
            login_shell=login_shell,
        )
        return await self.wait(job_id, timeout=timeout, sandbox_id=sandbox_id)

    async def submit(
        self,
        sandbox_id: str,
        command: str,
        *,
        timeout: int,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = False,
    ) -> str:
        """Start *command* and return its job id. Never sends it twice.

        A retry happens only when the request provably never left this host:
        the connection could not be opened, or the orchestrator answered 503
        without taking it. Every submit gets a fresh connection, because a
        pooled keep-alive connection the server has just closed fails exactly
        like a request the server received and dropped — and only the first is
        safe to repeat.
        """
        path = f"/sandboxes/{sandbox_id}/exec"
        payload = {
            "command": command,
            "timeout_seconds": timeout,
            "cwd": cwd,
            "env": env,
            "login_shell": login_shell,
        }
        bounds = aiohttp.ClientTimeout(
            total=SUBMIT_TIMEOUT_SEC, sock_connect=CONNECT_TIMEOUT_SEC
        )
        never_sent = (aiohttp.ClientConnectorError, *_CONNECT_TIMEOUTS)
        session = self._session(submit=True)
        for attempt in range(SUBMIT_ATTEMPTS):
            last = attempt == SUBMIT_ATTEMPTS - 1
            try:
                async with session.post(
                    self._base_url + path, json=payload, timeout=bounds
                ) as response:
                    if response.status == 503 and not last:
                        await asyncio.sleep(RETRY_AFTER_503_SEC * random.uniform(1, 1.4))
                        continue
                    response.raise_for_status()
                    return (await response.json())["job_id"]
            except never_sent:
                if last:
                    raise
                await asyncio.sleep(SUBMIT_BACKOFF_SEC * 2**attempt)
            except aiohttp.ClientResponseError:
                # The orchestrator answered, so nothing was dropped. A 404 is
                # a sandbox it no longer has; the environment names that.
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise ExecDispatchError(
                    f"POST {path} failed after it was sent ({type(exc).__name__}: "
                    f"{exc}). The command may or may not be running; it was not "
                    "sent again."
                ) from exc
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def wait(
        self,
        job_id: str,
        *,
        timeout: int,
        sandbox_id: str = "?",
        poll_interval: float = 0.1,
    ) -> JobResult:
        """Wait for a submitted job to finish, without ever running it again.

        A fast "still running" answer came from a replica that cannot hold the
        wait open, so it is followed by a pause that starts at *poll_interval*
        and doubles up to :data:`POLL_MAX_INTERVAL_SEC`.

        Raises:
            TimeoutError: the job is still unfinished ``timeout`` + 60s on,
                which the orchestrator's own command timeout should prevent.
            JobLostError: the orchestrator answered 404 for the job.
            JobWaitError: the orchestrator stayed unreachable too long.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout + 60
        pause = max(poll_interval, 0.0)
        outage_since: float | None = None
        session = self._session(submit=False)
        url = f"{self._base_url}/jobs/{job_id}/wait"
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")
            window = max(1, min(WAIT_WINDOW_SEC, int(remaining)))
            asked = loop.time()
            try:
                async with session.get(
                    url,
                    params={"timeout": window},
                    timeout=aiohttp.ClientTimeout(
                        total=window + 30, sock_connect=CONNECT_TIMEOUT_SEC
                    ),
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
            except aiohttp.ClientResponseError as exc:
                if exc.status == 404:
                    raise JobLostError(
                        f"the orchestrator has no record of job {job_id} on "
                        f"sandbox {sandbox_id}"
                    ) from exc
                if exc.status < 500:
                    raise
                failure: BaseException = exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                failure = exc
            else:
                result = JobResult(data)
                if result.is_complete:
                    return result
                outage_since = None
                if loop.time() - asked < window / 2:
                    await asyncio.sleep(pause)
                    pause = min(max(pause * 2, 0.05), POLL_MAX_INTERVAL_SEC)
                continue

            if outage_since is None:
                outage_since = asked
            if loop.time() - outage_since >= MAX_OUTAGE_SEC:
                raise JobWaitError(
                    f"lost the orchestrator for {MAX_OUTAGE_SEC}s while job "
                    f"{job_id} ran on sandbox {sandbox_id}: "
                    f"{type(failure).__name__}: {failure}"
                ) from failure
            await asyncio.sleep(POLL_MAX_INTERVAL_SEC)

    async def close(self) -> None:
        for session in (self._submit_session, self._wait_session):
            if session is not None and not session.closed:
                await session.close()
        self._submit_session = self._wait_session = None

    def _session(self, *, submit: bool) -> aiohttp.ClientSession:
        """The session for submits (never reused) or for waits (pooled)."""
        if submit:
            if self._submit_session is None or self._submit_session.closed:
                self._submit_session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(force_close=True, limit=200),
                    headers=self._headers,
                )
            return self._submit_session
        if self._wait_session is None or self._wait_session.closed:
            self._wait_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=200, keepalive_timeout=30),
                headers=self._headers,
            )
        return self._wait_session
