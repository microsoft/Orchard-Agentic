"""A task-shaped wrapper around one Orchard Env sandbox.

:class:`EvalSandbox` adds three things the raw SDK deliberately does not have:

* **repository operations** a SWE benchmark needs — reset to the base commit,
  extract the working diff, apply a candidate patch;
* **a sync bridge** (:meth:`EvalSandbox.exec_sync`) so a *synchronous* harness
  can drive an ``AsyncSandboxInstance`` from a worker thread while the runner
  stays asyncio-native;
* **failure semantics suited to evaluation** — a command that fails is a
  result, not an exception, unless the caller explicitly asks otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from orchard_env import AsyncSandboxInstance, JobResult

from orchard_evalkit.jobs import JobClient, JobTransportError

logger = logging.getLogger(__name__)

#: Paths a harness routinely leaves behind that must never reach the patch.
DEFAULT_PATCH_EXCLUDES = (
    "patch.txt",
    "*.orig",
    "*.rej",
    ".codex",
    ".pi",
    ".opencode",
    ".claude",
    ".aider*",
)

#: Verbatim from swebench's ``run_evaluation.GIT_APPLY_CMDS``, in order. A patch
#: counts as unappliable only once every one of these has failed, so an instance
#: scores zero here exactly when it would score zero upstream.
GIT_APPLY_CMDS = (
    "git apply --verbose",
    "git apply --verbose --3way",
    "git apply --verbose --reject",
    "patch --batch --forward --fuzz=5 -p1 -i",
)


class SandboxCommandError(RuntimeError):
    """A command that was required to succeed did not."""

    def __init__(self, command: str, result: JobResult):
        self.command = command
        self.result = result
        output = (result.stdout or "") + (result.stderr or "")
        super().__init__(
            f"command failed (exit={result.exit_code}): {command}\n{output[-2000:]}"
        )


class SandboxGoneError(RuntimeError):
    """The orchestrator no longer knows about this sandbox.

    A 404 on a sandbox that was created successfully means the pod was reaped
    mid-run — TTL expiry, eviction, node scale-down, an OOM-killed pod that
    reconciliation then dropped. Nothing inside it is recoverable, so this is
    raised as its own type rather than retried: the caller's only useful move is
    to record what it already has and mark the instance an infrastructure
    failure instead of an agent one.
    """

    def __init__(self, sandbox_id: str, cause: BaseException):
        self.sandbox_id = sandbox_id
        super().__init__(f"sandbox {sandbox_id} no longer exists ({cause})")


class SandboxUnusableError(RuntimeError):
    """The orchestrator could not run a command in this sandbox.

    Either it never got the command to the pod — the job ended with no exit
    code and an error that is not a timeout, such as "No pod IP available" —
    or an earlier command here already failed for that kind of reason, and
    nothing more is sent. Like :class:`SandboxGoneError` it is the cluster's
    failure, so the runner retries the instance in a fresh pod.
    """


def _http_status(exc: BaseException) -> int | None:
    """HTTP status carried by an SDK exception, from either transport."""
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _is_missing_sandbox(exc: BaseException) -> bool:
    """True for a 404 raised by either SDK transport (aiohttp or requests)."""
    return _http_status(exc) == 404


#: 4xx codes that still mean "try again": the orchestrator is shedding load or
#: the pod has not settled yet. Every other 4xx is the caller's fault and would
#: fail identically on a new pod.
RETRYABLE_CLIENT_STATUSES = frozenset({408, 409, 425, 429})

#: Base-class names used by the HTTP clients the SDK may be built on. Matching
#: by name avoids importing aiohttp/requests/httpx just to name their errors,
#: and keeps this working whichever transport the SDK is using.
_TRANSPORT_BASE_NAMES = frozenset(
    {"ClientError", "RequestException", "TransportError", "HTTPError"}
)

#: OSError subclasses that describe a *file*, not a connection. Without this,
#: a missing trajectory file would look like a dead pod and cost a full rollout.
_FILESYSTEM_ERRORS = (
    FileNotFoundError,
    FileExistsError,
    IsADirectoryError,
    NotADirectoryError,
    PermissionError,
)


def is_transport_error(exc: BaseException) -> bool:
    """True when the orchestrator or the pod's agent failed, not the command.

    A dropped connection or a 5xx says nothing about the patch under test, so
    it must be recorded as an infrastructure failure rather than folded into a
    resolve rate as an ordinary zero.
    """
    status = _http_status(exc)
    if status is not None:
        return status >= 500 or status in RETRYABLE_CLIENT_STATUSES
    if isinstance(exc, _FILESYSTEM_ERRORS):
        return False
    if isinstance(exc, JobTransportError | ConnectionError | TimeoutError):
        return True
    return any(base.__name__ in _TRANSPORT_BASE_NAMES for base in type(exc).__mro__)


def is_sandbox_failure(exc: BaseException) -> bool:
    """True when the sandbox is what failed — reaped, 5xx, or unreachable.

    This is the single predicate the runner uses to decide between *retry the
    whole instance on a fresh pod* and *record this as a result*. Anything the
    agent or the patch did is the latter; anything the cluster did is the
    former.
    """
    return isinstance(exc, SandboxGoneError | SandboxUnusableError) or (
        is_transport_error(exc)
    )


@dataclass
class ExecOutcome:
    """Plain-data view of a command result, safe to serialize into artifacts."""

    output: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class EvalSandbox:
    """One sandbox, bound to one task instance.

    Args:
        instance: A ready ``AsyncSandboxInstance`` from ``AsyncSandboxClient``.
        workdir: Where the repository lives inside the image (``/testbed``).
        loop: The event loop owning ``instance``. Captured at construction so
            :meth:`exec_sync` can hand work back to it from another thread.
        default_timeout: Seconds applied to commands that do not pass one.
        jobs: Runs the commands. Without one they go through ``instance.exec``,
            which is what the test doubles implement — but the SDK's ``exec``
            re-sends a command whose connection dropped, so anything that
            launches an agent must pass a :class:`JobClient`. The runner does.
    """

    def __init__(
        self,
        instance: AsyncSandboxInstance,
        *,
        workdir: str = "/testbed",
        loop: asyncio.AbstractEventLoop | None = None,
        default_timeout: int = 300,
        jobs: JobClient | None = None,
    ):
        self._instance = instance
        self.workdir = workdir
        self.default_timeout = default_timeout
        self._jobs = jobs
        #: The first infrastructure failure of a command here. Once set, no
        #: further command is sent: see :meth:`exec`.
        self._failure: BaseException | None = None
        # Only ``exec_sync`` needs a loop reference. Resolve it eagerly when we
        # are already on one, but never fail construction off-loop: an
        # EvalSandbox is perfectly usable via ``await exec()`` without this.
        if loop is not None:
            self._loop: asyncio.AbstractEventLoop | None = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None

    @property
    def sandbox_id(self) -> str:
        return self._instance.sandbox_id

    @contextlib.contextmanager
    def _translating_404(self) -> Iterator[None]:
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is a 404
            if _is_missing_sandbox(exc):
                raise SandboxGoneError(self.sandbox_id, exc) from exc
            raise

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def exec(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
        login_shell: bool = False,
        merge_stderr: bool = False,
    ) -> ExecOutcome:
        """Run ``command`` in the sandbox and return its combined output.

        Args:
            check: Raise :class:`SandboxCommandError` on a non-zero exit. Off by
                default — in evaluation a failing command is usually data.
            merge_stderr: Redirect stderr into stdout *inside the sandbox*. The
                agent returns the two as separate pipes, so joining them here
                puts all of stderr after all of stdout — which destroys the
                relative ordering of anything that writes to both.
        """
        if self._failure is not None:
            # A harness still reads the diff after a failed agent run. Sent
            # here, that command would queue behind whatever the failed call
            # left running — the orchestrator runs one exec per pod at a time —
            # for a result from a pod the runner is about to discard.
            raise SandboxUnusableError(
                f"not sent to sandbox {self.sandbox_id}: an earlier command there "
                f"already failed ({type(self._failure).__name__})"
            )
        if merge_stderr:
            command = f"{{\n{command}\n}} 2>&1"
        options = {
            "timeout": timeout or self.default_timeout,
            "cwd": cwd if cwd is not None else self.workdir,
            "env": env,
            "login_shell": login_shell,
        }
        try:
            with self._translating_404():
                if self._jobs is not None:
                    result = await self._jobs.run(self.sandbox_id, command, **options)
                else:
                    result = await self._instance.exec(command, **options)
        except Exception as exc:
            # A client-side deadline does not condemn the pod: it may be fine,
            # and the diff a timed-out agent left is still worth reading.
            if is_sandbox_failure(exc) and not isinstance(exc, TimeoutError):
                self._failure = exc
            raise
        error = (getattr(result, "error", None) or "").strip()
        if result.exit_code is None and error and "timed out" not in error.lower():
            # Read as exit -1 this was an agent failure scored on a broken pod.
            self._failure = SandboxUnusableError(
                f"the orchestrator could not run the command in sandbox "
                f"{self.sandbox_id}: {error}"
            )
            raise self._failure
        if check and not result.succeeded:
            raise SandboxCommandError(command, result)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        exit_code = result.exit_code if result.exit_code is not None else -1
        return ExecOutcome(
            output=stdout + stderr,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    def exec_sync(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = False,
        merge_stderr: bool = False,
    ) -> ExecOutcome:
        """Blocking :meth:`exec`, callable only from a non-loop thread.

        Synchronous harnesses (anything built around a blocking ``execute()``)
        run inside ``asyncio.to_thread``; this hands the coroutine back to the
        runner's loop and waits for it. Calling it *on* the loop thread would
        deadlock, so that is rejected outright.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is self._loop:
            raise RuntimeError(
                "exec_sync() called from the event loop thread; "
                "use `await sandbox.exec(...)` instead."
            )
        if self._loop is None:
            raise RuntimeError(
                "exec_sync() needs the event loop that owns this sandbox. "
                "Construct EvalSandbox from within the loop, or pass loop=..."
            )

        future = asyncio.run_coroutine_threadsafe(
            self.exec(
                command,
                timeout=timeout,
                cwd=cwd,
                env=env,
                login_shell=login_shell,
                merge_stderr=merge_stderr,
            ),
            self._loop,
        )
        # The orchestrator already enforces `timeout` server-side; the extra
        # margin here only guards against a lost response.
        return future.result(timeout=(timeout or self.default_timeout) + 120)

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    async def _retry_transport(self, what: str, call, attempts: int = 4):
        """Retry ``call`` while it fails for reasons the sandbox is not to blame for.

        File transfers go through the orchestrator to the in-pod agent, and a
        blip on either hop otherwise aborts an entire graded instance — which
        then reads as a failed patch rather than a failed cluster.
        """
        for attempt in range(1, attempts + 1):
            try:
                with self._translating_404():
                    return await call()
            except SandboxGoneError:
                raise
            except Exception as exc:  # noqa: BLE001 - re-raised unless transient
                if attempt == attempts or not is_transport_error(exc):
                    raise
                delay = min(2**attempt, 15) * (0.5 + random.random())
                logger.warning(
                    "%s failed on %s (attempt %d/%d): %s; retrying in %.1fs",
                    what,
                    self.sandbox_id,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    async def write_file(self, content: str | bytes, remote_path: str) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        await self._retry_transport(
            f"upload {remote_path}",
            lambda: self._instance.upload_content(data, remote_path),
        )

    async def read_file(self, remote_path: str) -> str:
        data = await self._retry_transport(
            f"download {remote_path}",
            lambda: self._instance.download_content(remote_path),
        )
        return data.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Repository operations
    # ------------------------------------------------------------------

    async def prepare_repo(self, base_commit: str, *, reset: bool = True) -> None:
        """Make git usable in the pod, optionally restoring ``base_commit``.

        ``safe.directory`` has to be set before git will touch a tree it does
        not own. That part always runs.

        Args:
            reset: Restore tracked files to ``base_commit``. **Never do this in
                a grading pod.** SWE-bench images carry uncommitted working-tree
                edits made by their own build — sphinx's ``pre_install`` seds
                ``-rA`` into ``tox.ini``, and without it pytest prints progress
                dots instead of the ``PASSED <test>`` lines the log parser reads,
                so a passing suite scores zero. Resetting is only useful in a
                rollout pod, where it keeps the image builder's edits out of the
                extracted diff.

        Only *tracked* content and untracked-but-not-ignored files are reset.
        Ignored files are part of the prebuilt environment — compiled C
        extensions, ``version.py`` generated by setuptools-scm, ``*.egg-info``
        — and deleting them uninstalls the package under test, which turns
        every subsequent test run into a collection error.
        """
        await self.exec(
            f"git config --global --add safe.directory {shlex.quote(self.workdir)}",
            timeout=60,
        )
        if reset and base_commit:
            await self.exec(
                f"git checkout -f {shlex.quote(base_commit)} -- . "
                f"&& git reset {shlex.quote(base_commit)} -- . "
                "&& git clean -fd",
                timeout=180,
            )

    async def extract_patch(
        self,
        base_commit: str,
        *,
        excludes: tuple[str, ...] = DEFAULT_PATCH_EXCLUDES,
        timeout: int = 180,
    ) -> str:
        """Return the diff the harness produced, as a unified patch.

        Uses ``git add -A`` so newly created source files are included, then
        diffs the index against ``base_commit``. ``excludes`` drops the scratch
        files agents habitually leave behind; the benchmark's own test files are
        restored by the eval script regardless, so they need no special care.

        The excludes are applied to the ``git add`` as well as the ``git diff``.
        An agent that ran to its timeout can leave gigabytes of caches and
        virtualenvs in the tree, and staging those only to discard them at diff
        time is what pushes this command past its timeout — which loses the
        patch entirely.

        Raises:
            SandboxGoneError: The pod was reaped before the diff could be read.
                Distinct from an empty patch, which is an ordinary result.
        """
        pathspecs = " ".join(f"':(exclude){e}'" for e in excludes)
        ref = shlex.quote(base_commit) if base_commit else "HEAD"
        command = (
            f"git add -A -- . {pathspecs} >/dev/null 2>&1; "
            f"git diff --no-color --cached {ref} -- . {pathspecs}"
        )
        result = await self.exec(command, timeout=timeout)
        if not result.succeeded and not result.stdout.strip():
            logger.warning(
                "patch extraction failed in %s: %s",
                self.sandbox_id,
                result.stderr[:500],
            )
            # Last resort: tracked files only, no staging. This misses newly
            # created files, but a partial patch scores better than none when
            # the full walk could not finish.
            fallback = await self.exec(
                f"git diff --no-color {ref} -- . {pathspecs}", timeout=60
            )
            return fallback.stdout if fallback.succeeded else ""
        return result.stdout

    async def apply_patch(self, patch: str, *, timeout: int = 120) -> ExecOutcome:
        """Apply a candidate patch the way the official harness does.

        Each command in :data:`GIT_APPLY_CMDS` is tried in turn, and the tree is
        restored between attempts — a failed ``git apply`` (``--reject`` above
        all) leaves partial state behind that makes every later attempt fail on
        a patch that would otherwise have applied. If the whole chain fails, the
        patch is checked in reverse, because the chain can leave it fully
        applied while every command still exited non-zero.
        """
        if not patch.endswith("\n"):
            patch += "\n"
        remote = "/tmp/orchard_eval_model.patch"
        await self.write_file(patch, remote)

        result = ExecOutcome(output="", exit_code=1)
        transcript: list[str] = []
        for attempt, apply_cmd in enumerate(GIT_APPLY_CMDS):
            if attempt:
                await self.exec(
                    "git checkout -- . ; git clean -fd",
                    timeout=timeout,
                    merge_stderr=True,
                )
            result = await self.exec(
                f"{apply_cmd} {remote}", timeout=timeout, merge_stderr=True
            )
            transcript.append(f"$ {apply_cmd} {remote}\n{result.output}")
            if result.succeeded:
                return ExecOutcome(
                    output="\n".join(transcript),
                    exit_code=0,
                    stdout=result.stdout,
                )

        reverse = await self.exec(
            f"git apply --check --reverse {remote}", timeout=timeout, merge_stderr=True
        )
        transcript.append(f"$ git apply --check --reverse\n{reverse.output}")
        return ExecOutcome(
            output="\n".join(transcript),
            exit_code=0 if reverse.succeeded else result.exit_code,
        )

    # ------------------------------------------------------------------

    async def probe_harness(self, binary: str) -> str | None:
        """Return the resolved path of an in-sandbox CLI, or ``None``."""
        result = await self.exec(f"command -v {shlex.quote(binary)}", timeout=60)
        path = result.stdout.strip()
        return path or None

    async def verify_harness(
        self,
        path: str,
        args: Sequence[str] = ("--version",),
        *,
        timeout: int = 120,
    ) -> ExecOutcome:
        """Run an already-resolved CLI, to prove it can actually start.

        :meth:`probe_harness` answers "is there a file here", which is a weaker
        question than it looks: the sandbox tools are dynamically linked ELFs
        mounted into someone else's image, so on a musl (Alpine) sandbox the
        wrapper resolves, ``execve`` fails on the missing glibc loader, and the
        shell reports exit 127 — hundreds of rollouts into the run. Executing
        the thing is the only probe that catches that before the agent starts.
        """
        command = " ".join(shlex.quote(token) for token in (path, *args))
        return await self.exec(command, timeout=timeout, merge_stderr=False)

    def serialize(self) -> dict[str, Any]:
        return {"sandbox_id": self.sandbox_id, "workdir": self.workdir}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EvalSandbox({self.sandbox_id} @ {self.workdir})"
