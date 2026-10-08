"""Shared fakes for the offline test suite.

Everything here exists so the suite can be tested without an orchestrator, a
model API, or the optional ``swebench`` package. The fakes mimic only the
surface the code under test actually touches.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeJobResult:
    """Stand-in for ``orchard_env.JobResult``."""

    stdout: str = ""
    stderr: str = ""
    #: ``None`` when the orchestrator ended the job without one, as it does on
    #: a timeout or when it could not reach the pod; ``error`` then says which.
    exit_code: int | None = 0
    status: str = "succeeded"
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class FakeResponseError(Exception):
    """Shape of ``aiohttp.ClientResponseError``: the status is an attribute."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"{status}, message='Not Found'")


@dataclass
class FakeSandboxInstance:
    """Stand-in for ``AsyncSandboxInstance``.

    ``responder`` maps a command to a :class:`FakeJobResult`; anything it does
    not recognize succeeds with empty output, which keeps tests focused on the
    one or two commands they actually care about.
    """

    sandbox_id: str = "sbx-test"
    responder: Callable[[str], FakeJobResult] | None = None
    commands: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    deleted: bool = False

    async def exec(
        self,
        command,
        timeout=None,
        cwd=None,
        env=None,
        login_shell=False,
        **kwargs,
    ) -> FakeJobResult:
        self.commands.append(
            {
                "command": command,
                "timeout": timeout,
                "cwd": cwd,
                "env": env,
                "login_shell": login_shell,
            }
        )
        if self.responder is not None:
            result = self.responder(str(command))
            if result is not None:
                return result
        return FakeJobResult()

    async def upload_content(self, content: bytes, remote_path: str) -> dict:
        self.files[remote_path] = content
        return {"ok": True}

    async def download_content(self, remote_path: str) -> bytes:
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        return self.files[remote_path]

    async def delete(self) -> None:
        self.deleted = True


class FakeSandboxClient:
    """Stand-in for ``AsyncSandboxClient`` as an async context manager."""

    base_url = "http://fake-orchestrator"
    api_key = None

    def __init__(self, *, fail_times: int = 0, **_: Any):
        self.created: list[FakeSandboxInstance] = []
        self.fail_times = fail_times
        self.create_calls = 0

    async def __aenter__(self) -> FakeSandboxClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def create_sandbox(self, image: str, **_: Any) -> FakeSandboxInstance:
        self.create_calls += 1
        if self.create_calls <= self.fail_times:
            raise RuntimeError("simulated pod creation failure")
        instance = FakeSandboxInstance(sandbox_id=f"sbx-{self.create_calls}")
        self.created.append(instance)
        return instance


class FakeJobClient:
    """Stand-in for ``orchard_evalkit.jobs.JobClient``.

    Runs each command on the fake instance it names, so the instance's
    ``commands`` and ``responder`` work exactly as they do without one.
    """

    def __init__(self, instances: Callable[[], list[FakeSandboxInstance]]):
        self._instances = instances

    async def run(self, sandbox_id: str, command, **options: Any) -> FakeJobResult:
        instance = next(i for i in self._instances() if i.sandbox_id == sandbox_id)
        return await instance.exec(command, **options)

    async def close(self) -> None:
        return None


def run_in_thread(coro_fn: Callable[[], Any]) -> Any:
    """Run a blocking callable off the event loop, like the runner does."""
    return asyncio.to_thread(coro_fn)
