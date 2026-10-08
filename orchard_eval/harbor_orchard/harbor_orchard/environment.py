"""A Harbor environment provider backed by Orchard Env sandboxes.

Harbor's ``BaseEnvironment`` is small on purpose — start, stop, exec, and four
transfer methods — and every capability beyond that is declared rather than
assumed. That declaration is what makes this provider safe: a task needing
Docker Compose, GPUs, or a ``no-network`` baseline is refused by Harbor before
a pod is created, instead of running in an environment that quietly does not
match what the task author specified and scoring the model on it.

Two capabilities are declared ``False`` deliberately and are worth naming:

``mounted``
    Orchard cannot bind-mount host directories. Declaring this makes Harbor
    *download* ``/logs`` after each phase rather than expecting it to appear on
    the host — the framework already has both paths, so "no mounts" costs
    nothing but the declaration.

``docker_compose``
    A task with an ``environment/docker-compose.yaml`` needs several networked
    containers. One pod cannot be several containers, and pretending otherwise
    would fail the task at verification time for reasons that look like a model
    error.

``disable_internet`` is declared ``True``, which it was not when only
terminal-bench ran here. Whole benchmarks depend on it — every DeepSWE task is
``network_mode = "no-network"`` — and declaring ``False`` makes Harbor refuse
all 113 at load time.

So is ``dynamic_network_policy``, and that one is not optional either. Harbor
scopes network policy to *phases*: the pod starts on the ``[environment]``
baseline (``public`` unless the task says otherwise) so ``agent.setup()`` can
install an agent CLI, and Harbor then calls ``set_network_policy`` to apply the
``[agent]`` override before ``agent.run()``. DeepSWE declares exactly that shape
— no ``[environment].network_mode``, ``no-network`` on ``[agent]`` and
``[verifier]`` — so a provider that cannot switch is rejected at trial init with
"network policy differs from the baseline". ``_apply_network_policy`` below is
the hook, and ``PUT /sandboxes/{id}/network`` is what makes it possible: egress
is otherwise fixed when the pod is created.

That route has carried two payload shapes, so ``_switch_network`` dispatches on
what the SDK exposes rather than pinning one — ``disable_network`` /
``enable_network`` with per-port rules, or the older ``set_network`` with bare
CIDRs. An orchestrator offering neither is named as such, because the failure is
a deployment to fix and not something a retry will help.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from typing import override

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.definition import (
    COMPOSE_FILE_NAME,
    DOCKERFILE_NAME,
    require_agent_environment_definition,
    should_use_prebuilt_docker_image,
)
from harbor.models.task.config import NetworkMode, NetworkPolicy
from orchard_evalkit.jobs import JobClient, JobTransportError

from harbor_orchard import progress, transfer
from harbor_orchard.agent_config import (
    CODEX_CONFIG_PATH,
    CODEX_HOME,
    PI_CONFIG_PATH,
    PI_HOME,
    codex_config,
    pi_config,
)
from harbor_orchard.builder import BuildError, SandboxBuilder
from harbor_orchard.dockerfile import parse_file
from harbor_orchard.image_config import fetch_image_workdir
from harbor_orchard.network import allow_rules_for, cidrs_of_rules, with_resolved_host
from harbor_orchard.plan import StageState
from harbor_orchard.router import close_session
from harbor_orchard.settings import (
    AGENT_KILL_GRACE_S,
    BASE_URL_ENV_VARS,
    DEADLINE_ENV_VAR,
    PAYLOAD_DIR,
    ConfigurationError,
    agent_deadline,
    anthropic_base_url,
    load_settings,
    task_agent_timeout_sec,
)
from harbor_orchard.shell import PREFER_IPV4_ROUTINE, wrap_user

# Here because this module is imported exactly when Harbor loads the provider,
# in Harbor's own process, before the first trial can finish — and because it
# importing at all means Harbor is installed. The live progress bar otherwise
# reports whichever reward key sorts first, which on DeepSWE is `f2p` and not
# the score; see `progress` for what that costs a run.
progress.install()

ENVIRONMENT_TYPE = "orchard"

#: Seconds one liveness probe may take. The client's own session timeout is
#: sized for an agent loop, so a probe left to it would outlast what it checks.
LIVENESS_PROBE_TIMEOUT = 30

#: What ``timeout(1)`` exits with when it fires, GNU and busybox alike.
TIMEOUT_EXIT_CODE = 124


class SandboxInfraError(RuntimeError):
    """The sandbox or the orchestrator failed under this trial.

    Raised instead of returning whatever the failed call left behind, because
    nothing about such a trial measures the model, and continuing in the same
    pod is how a second agent came to start on a tree the first one had already
    edited. The class name is what ``orchard-eval harbor`` passes to Harbor's
    ``--retry-include``: Harbor then discards the trial and runs it again as a
    new one — a new pod, a clean checkout, and a full agent budget.
    """


class SandboxGone(SandboxInfraError):
    """The pod backing this environment no longer exists."""


#: Statuses the orchestrator returns for a failure of its own rather than of
#: the request: overloaded, timed out, or unable to reach the pod.
INFRA_STATUSES = frozenset({408, 429})


def is_infra_failure(exc: BaseException) -> bool:
    """True when the orchestrator or the pod failed, not the command or the task.

    A command that exits non-zero is a result and never matches. A 4xx is the
    caller's mistake and fails identically on a new pod, so it does not match
    either; a 404 is handled before this by the callers, as a reaped pod.
    """
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status >= 500 or status in INFRA_STATUSES
    if isinstance(exc, JobTransportError | ConnectionError | TimeoutError):
        return True
    # aiohttp's connection errors, matched by name so this module does not need
    # to import the transport the SDK happens to be built on.
    return any(base.__name__ == "ClientError" for base in type(exc).__mro__)


#: Seconds for the pre-collect commit. Generous because ``git add -A`` on a
#: tree an agent has been building in for an hour walks every untracked file
#: the repository does not ignore, and a commit that times out is a rollout
#: graded as an empty patch.
COMMIT_TIMEOUT_SEC = 300

#: Stage and commit the worktree in the first of *dirs* that is a git
#: repository. ``safe.directory`` first, because the agent may have run as a
#: different user than this exec does — the collect hook sets the same thing
#: for the same reason. ``--no-verify`` skips hooks a repository installs for
#: its developers; ``-c user.*`` supplies the identity task images omit, so
#: this works whether or not the agent ever configured one.
COMMIT_WORKTREE = """\
set -u
for d in {dirs}; do
    [ -d "$d" ] || continue
    git config --global --add safe.directory "$d" >/dev/null 2>&1 || true
    git -C "$d" rev-parse --git-dir >/dev/null 2>&1 || continue
    git -C "$d" add -A >/dev/null 2>&1 || true
    if git -C "$d" diff --cached --quiet >/dev/null 2>&1; then
        echo "$d: nothing to commit"
        exit 0
    fi
    git -C "$d" -c user.name={name} -c user.email={email} \
        commit --no-verify -q -m "orchard-eval: agent work at end of rollout" \
        >/dev/null 2>&1 \
        && echo "$d: committed" || echo "$d: commit failed"
    exit 0
done
exit 0
"""

#: Seconds for the resolver fix-up. Two small file writes; anything slower than
#: this is a pod that is not answering, and the trial is better off without it.
PREFER_IPV4_TIMEOUT_SEC = 60


class OrchardEnvironment(BaseEnvironment):
    """One Orchard sandbox pod, standing in for one container image."""

    def __init__(self, *args, **kwargs):
        self._settings = load_settings()
        super().__init__(*args, **kwargs)
        self._client = None
        self._instance = None
        self._state = StageState()
        self._owns_client = False
        self._endpoint: str | None = None
        self._warned_internet = False
        self._warned_isolation = False
        #: The pod's current (blocked, allow rules), to skip no-op switches.
        self._pod_network: tuple[bool, list[dict]] | None = None
        #: One pre-collect commit per trial, however many hooks run.
        self._committed_before_collect = False
        #: Computed from the task's own ``task.toml`` on first use, then kept:
        #: it cannot change within a trial and a rollout issues hundreds of
        #: execs. ``_deadline_resolved`` separates "not looked up yet" from
        #: "looked up, and this task has no deadline".
        self._deadline_sec: int | None = None
        self._deadline_resolved = False
        #: Live :meth:`_stop_agent_cli` tasks, held so the loop does not drop
        #: them before they run.
        self._stop_tasks: set[asyncio.Task] = set()
        #: The first infrastructure failure of an exec in this pod. Once set,
        #: no further command is sent here: see :meth:`exec`.
        self._failure: SandboxInfraError | None = None
        #: Runs every command Harbor sends; see :mod:`orchard_evalkit.jobs`.
        self._jobs: JobClient | None = None

    # ------------------------------------------------------------------
    # Declarations
    # ------------------------------------------------------------------

    @staticmethod
    @override
    def type() -> str:
        return ENVIRONMENT_TYPE

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            gpus=False,
            tpus=False,
            # Honoured at pod creation, and again whenever Harbor switches
            # phase; see _apply_network_policy.
            disable_internet=True,
            # Harbor's allowlist mode names *hosts*, which a pod-level
            # NetworkPolicy cannot express — it allows addresses. Declaring
            # False rejects such a task instead of enforcing a weaker rule than
            # the author asked for. Neither DeepSWE nor terminal-bench uses it.
            network_allowlist=False,
            dynamic_network_policy=True,
            windows=False,
            mounted=False,
            docker_compose=False,
        )

    @classmethod
    @override
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # Kubernetes requests and limits are both set from the same value by the
        # orchestrator, so a task's cpus/memory_mb are honoured either way.
        return EnvironmentResourceCapabilities(
            cpu_limit=True,
            cpu_request=True,
            memory_limit=True,
            memory_request=True,
        )

    @classmethod
    def preflight(cls) -> None:
        """Fail before a job starts rather than on its first trial."""
        load_settings().require_base_url()

    @override
    def _validate_definition(self) -> None:
        """Reject a task this provider cannot honour, at construction time.

        Harbor only rejects compose tasks itself when they *reference* sidecar
        services from artifacts or collect hooks. A task that merely ships an
        ``environment/docker-compose.yaml`` would otherwise start here as a
        single container and fail much later, during verification, for reasons
        that look like a model error.
        """
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
        )
        for name in (COMPOSE_FILE_NAME, "docker-compose.yml"):
            if (self.environment_dir / name).exists():
                raise ConfigurationError(
                    f"{self.environment_dir / name} needs several networked "
                    "containers, which a single Orchard sandbox cannot provide."
                )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @override
    async def start(self, force_build: bool) -> None:
        try:
            await self._start(force_build)
        except (ConfigurationError, BuildError, SandboxInfraError):
            raise
        except Exception as exc:
            # A pod that could not be created or set up is the cluster's
            # failure, and a 404 this early means the sandbox vanished before
            # the trial began — neither is the task's.
            if getattr(exc, "status", None) == 404 or is_infra_failure(exc):
                raise self._infra_error(exc) from exc
            raise

    async def _start(self, force_build: bool) -> None:
        self._pin_endpoint()
        await self._connect()
        builder = self._make_builder()

        dockerfile_path = self.environment_dir / DOCKERFILE_NAME
        prebuilt = should_use_prebuilt_docker_image(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
            force_build=force_build,
        )

        if prebuilt or not dockerfile_path.exists():
            reference = self.task_env_config.docker_image
            if not reference:
                raise ConfigurationError(
                    f"{self.environment_dir} has no Dockerfile and the task sets no "
                    "[environment].docker_image, so there is no image to start."
                )
            image = builder.resolve_image(reference)
            self.logger.info("starting %s from prebuilt image %s", self.session_id, image)
            self._instance = await self._create_sandbox(image)
            self._state = StageState(
                env=await builder.read_environment(self._instance),
                workdir=await self._prebuilt_workdir(image),
            )
        else:
            dockerfile = parse_file(dockerfile_path)
            image = builder.resolve_image(dockerfile.final_stage.base)
            self.logger.info(
                "starting %s from %s and replaying %s",
                self.session_id,
                image,
                dockerfile_path,
            )
            self._instance = await self._create_sandbox(image)
            try:
                result = await builder.build(
                    self._instance, dockerfile, context_dir=self.environment_dir
                )
            except BuildError as exc:
                # The pod is a failed build, not a usable environment. Leaving it
                # running would let the trial proceed against a half-built image.
                await self.stop(delete=True)
                self._explain_build_failure(exc)
                raise
            self._state = result.state
            self.logger.info(
                "%s built in %d steps", self.session_id, result.step_count
            )

        # Harbor bind-mounts /logs/{agent,verifier,artifacts} for providers that
        # can; a mount-less one has to materialize them itself. Without this the
        # oracle's `> /logs/agent/oracle.txt` redirect fails, solve.sh never
        # runs, and the trial dies later with RewardFileNotFoundError.
        await self.ensure_dirs(self._mount_targets(writable_only=True))

        await self._prefer_ipv4()
        await self._stage_agent_config()

        # Task-declared env vars are already resolved into _persistent_env by
        # the base class, and applied by _merge_env on every exec.
        await self._upload_environment_dir_after_start()

    @override
    async def stop(self, delete: bool) -> None:
        instance, self._instance = self._instance, None
        if instance is not None and delete:
            try:
                await instance.delete()
            except Exception as exc:  # noqa: BLE001 - teardown is best effort
                self.logger.debug("failed to delete %s: %s", self.session_id, exc)
        jobs, self._jobs = self._jobs, None
        if jobs is not None:
            try:
                await jobs.close()
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("failed to close the job client: %s", exc)
        if self._owns_client and self._client is not None:
            try:
                await self._client.close(cleanup=False)
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("failed to close sandbox client: %s", exc)
            self._client = None
            self._owns_client = False
        # Last, and never at the expense of the teardown above. A pod that is
        # not deleted stays charged to the run, while a session the router is
        # not told about only holds its engine until the idle window expires —
        # so if one of the two has to be skipped it is this one.
        try:
            await close_session(self._endpoint, logger=self.logger)
        except Exception as exc:  # noqa: BLE001 - advisory, never fatal
            self.logger.debug("failed to close the router session: %s", exc)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        instance = self._require_instance()
        if self._failure is not None:
            # Harbor still collects the patch and artifacts from a trial that
            # failed. Sent here, each command would queue behind whatever the
            # failed call left running — the orchestrator runs one exec per pod
            # at a time — for a result from a pod the trial is discarding.
            raise self._infra_error(
                f"not sent, because an earlier command already failed in this "
                f"pod ({type(self._failure.__cause__ or self._failure).__name__})"
            )

        # The image's own environment sits underneath everything, exactly as
        # `docker exec` inherits it. Without this, a verifier invoked after a
        # Dockerfile that extended PATH would not find the tools it installed.
        merged = dict(self._state.env)
        overlay = self._merge_env(env)
        if overlay:
            merged.update(overlay)

        # Last, so the pinned endpoint wins over whatever the agent was
        # configured with globally — that override is the whole point of it.
        pinned = self._endpoint_env()
        if pinned:
            merged.update(pinned)
            # Codex aborts when its env_key resolves to nothing, and a server
            # started without --api-key accepts any value.
            merged.setdefault("OPENAI_API_KEY", "EMPTY")
            # Claude Code with no credential falls through to looking for
            # stored OAuth credentials, finds none, and exits asking to be
            # logged in — under `--print` there is nobody to ask. Harbor's own
            # resolution reads the *host* environment, which a benchmark run
            # deliberately strips, so a run against a server started without
            # --api-key would otherwise have no token at all.
            merged.setdefault("ANTHROPIC_AUTH_TOKEN", "EMPTY")

        # Read only by the agent shims in `agents.py`, so exporting it on every
        # exec is harmless: nothing else in the pod looks at it, and the shim
        # is on the one path that needs bounding.
        deadline = self._agent_deadline_sec()
        if deadline is not None:
            merged[DEADLINE_ENV_VAR] = str(deadline)

        effective_user = user if user is not None else self.default_user
        if effective_user is None:
            effective_user = self._state.user

        effective_timeout = timeout_sec or self._settings.exec_timeout
        dispatched = time.monotonic()
        try:
            # Not `instance.exec`: the SDK holds one POST open for the whole
            # command and sends it again when that connection drops, which
            # started a second agent CLI in this pod. The job client sends a
            # command once and waits on its job id instead.
            result = await self._guard(
                self._job_client().run(
                    instance.sandbox_id,
                    wrap_user(command, _as_user(effective_user)),
                    timeout=effective_timeout,
                    cwd=cwd or self.task_env_config.workdir or self._state.workdir,
                    env=merged,
                )
            )
        except asyncio.CancelledError:
            # Harbor's agent deadline is a `wait_for` around the coroutine that
            # leads here, so a cancellation at this point means Harbor has
            # stopped waiting while the command is still running in the pod.
            # The shim's own deadline should already have ended it; this covers
            # the cases where it could not — an image with no `timeout`, a CLI
            # that outlived SIGKILL's grace, a cancellation for some other
            # reason entirely. Without it, every call Harbor makes next queues
            # behind a process nobody is reading any more.
            await self._stop_agent_cli()
            raise
        except SandboxInfraError as exc:
            self._failure = exc
            raise
        # An exec that takes minutes is either the agent loop itself — which is
        # supposed to — or the orchestrator taking that long to start or return
        # one, which would be invisible otherwise. Harbor's trial.log has no
        # timestamps, so the duration has to be in the message. Logged above a
        # threshold rather than always, because a rollout issues hundreds.
        held = time.monotonic() - dispatched
        if held >= self._settings.slow_exec_log_sec:
            # The orchestrator stamps the job three times, and the gaps between
            # them split this window where no other signal can: `created`
            # (request accepted) -> `started` (the command began in the pod) ->
            # `completed`. A long queue is the orchestrator's problem; a long
            # run with a prompt start is the agent's. Two trials beginning in
            # the same second have measured 0.1 and 53 minutes to their first
            # model call, so the difference is per-trial and lives in here.
            queued = ran = None
            created_at = getattr(result, "created_at", None)
            started_at = getattr(result, "started_at", None)
            completed_at = getattr(result, "completed_at", None)
            if created_at and started_at:
                queued = started_at - created_at
            if started_at and completed_at:
                ran = completed_at - started_at
            self.logger.info(
                "%s: exec held %.0fs (queued %s, ran %s, limit %ss): %s",
                self.session_id,
                held,
                f"{queued:.0f}s" if queued is not None else "?",
                f"{ran:.0f}s" if ran is not None else "?",
                effective_timeout,
                command.strip().splitlines()[0][:80] if command.strip() else "",
            )

        exit_code = result.exit_code
        stderr = result.stderr
        error = (getattr(result, "error", None) or "").strip()
        if exit_code is None and error and "timed out" not in error.lower():
            # The orchestrator never got the command to the pod, or lost it
            # there — "No pod IP available", "agent connection failed". Read as
            # exit -1 this was an agent failure scored on a broken pod.
            self._failure = self._infra_error(
                f"the orchestrator could not run the command in the pod: {error}"
            )
            raise self._failure
        if exit_code is None:
            # The job never finished, so there is no exit code to report. Harbor
            # reads the -1 below as a crashed agent; say what actually happened.
            note = (
                f"orchard: command did not finish within {effective_timeout}s "
                "(ORCHARD_HARBOR_EXEC_TIMEOUT); the orchestrator stopped waiting "
                "on it. Harbor reports this as exit -1."
            )
            self.logger.warning("%s: %s", self.session_id, note)
            stderr = f"{stderr}\n{note}" if stderr else note
            exit_code = -1
        elif exit_code == TIMEOUT_EXIT_CODE and deadline is not None:
            # `timeout` in the shim, not the agent. Harbor raises
            # NonZeroAgentExitCodeError for any non-zero exit, and the summary
            # in `orchard_evalkit.harbor_bridge` reads that as AGENT_ERROR —
            # which would hide a budget that is simply too small behind a
            # category meaning "the CLI is broken". Naming it in stderr is what
            # puts it back in TIMEOUT, where it can be counted.
            #
            # Worded to avoid "timed out": Harbor's own ERROR_PATTERNS map that
            # phrase to NetworkConnectionError.
            note = (
                f"orchard: the agent CLI was stopped at its in-pod deadline of "
                f"{deadline}s (ORCHARD_HARBOR_AGENT_MARGIN before Harbor's own), "
                f"so its transcript and diff are intact. Harbor reports this as "
                f"exit {TIMEOUT_EXIT_CODE}."
            )
            self.logger.warning("%s: %s", self.session_id, note)
            stderr = f"{stderr}\n{note}" if stderr else note

        return ExecResult(stdout=result.stdout, stderr=stderr, return_code=exit_code)

    @override
    async def service_exec(
        self,
        command: str,
        *,
        service: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        """Run *command*, committing the worktree first if it reads ``HEAD``.

        Harbor reaches this method for one thing only: a task's
        ``[[verifier.collect]]`` hooks, run once the agent is done. That makes
        it the one point in the trial that is both after the agent and before
        anything reads what the agent produced — including on the timeout path,
        where the agent coroutine is cancelled and never gets to run cleanup of
        its own. Since most DeepSWE rollouts end exactly that way, a commit
        anywhere inside the agent would miss the cases that need it most.
        """
        if self._reads_committed_history(command):
            await self._commit_worktree()
        return await super().service_exec(
            command,
            service=service,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            user=user,
        )

    def _reads_committed_history(self, command: str) -> bool:
        """True for a collect hook that grades ``HEAD`` rather than the tree.

        Matched on shape rather than on the task name so this needs no list to
        maintain: DeepSWE 1.1's hook is ``git diff --binary <base> HEAD``, and
        any hook written the same way has the same blind spot. A task with no
        collect hooks (SWE-bench Pro, Terminal-Bench 2.1 — both grade the live
        container) never reaches here at all.
        """
        if not self._settings.commit_before_collect:
            return False
        if self._committed_before_collect:
            return False
        return "git diff" in command and "HEAD" in command

    async def _commit_worktree(self) -> None:
        """Commit whatever the agent left behind, best-effort.

        A no-op when the agent committed for itself: ``git add -A`` stages
        nothing and the ``--cached --quiet`` check short-circuits. Excludes are
        left to the repository's own ``.gitignore`` rather than a list of our
        own, so what lands in the diff is what the project already considers
        source.

        Never raises. A trial that cannot commit is exactly as gradable as it
        was before this ran, and failing the trial here would turn a missing
        improvement into a lost rollout.
        """
        self._committed_before_collect = True
        candidates = [
            d
            for d in (self.task_env_config.workdir, self._state.workdir, "/app")
            if d and d != "/"
        ]
        seen: list[str] = []
        for directory in candidates:
            if directory not in seen:
                seen.append(directory)
        if not seen:
            return
        script = COMMIT_WORKTREE.format(
            dirs=" ".join(shlex.quote(d) for d in seen),
            name=shlex.quote(self._settings.commit_author_name),
            email=shlex.quote(self._settings.commit_author_email),
        )
        try:
            result = await self.exec(script, timeout_sec=COMMIT_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 - never fail a trial over this
            self.logger.warning("%s: pre-collect commit failed: %s", self.session_id, exc)
            return
        if result.stdout.strip():
            self.logger.info(
                "%s: pre-collect commit: %s", self.session_id, result.stdout.strip()
            )

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        await self._guard(
            transfer.upload_file(
                self._require_instance(),
                Path(source_path),
                target_path,
                chunk_size=self._settings.chunk_size,
            )
        )

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await self._guard(
            transfer.upload_dir(
                self._require_instance(),
                Path(source_dir),
                target_dir,
                chunk_size=self._settings.chunk_size,
            )
        )

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        await self._guard(
            transfer.download_file(
                self._require_instance(),
                source_path,
                Path(target_path),
                chunk_size=self._settings.chunk_size,
            )
        )

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        await self._guard(
            transfer.download_dir(
                self._require_instance(),
                source_dir,
                Path(target_dir),
                chunk_size=self._settings.chunk_size,
            )
        )

    @override
    async def attach(self) -> None:
        instance = self._require_instance()
        raise NotImplementedError(
            "Attach to this sandbox with the Orchard SDK: "
            f"sandbox_id={instance.sandbox_id}"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_instance(self):
        if self._instance is None:
            raise RuntimeError(
                f"environment {self.session_id} is not running; call start() first"
            )
        return self._instance

    async def _guard(self, awaitable):
        """Say "the pod is gone" when it is, rather than waiting out the timeout.

        The orchestrator drops a sandbox record once its pod disappears, so a
        pod evicted or reaped mid-trial answers every later call with a bare
        404 — which reads as a bug in the transfer layer rather than as the
        cluster having taken the environment away.

        A call already in flight when the pod goes is worse: nothing on the
        orchestrator fails the job it is waiting on, so it blocks for the full
        exec timeout — hours, on a benchmark whose agent budget is hours, with
        no output while it does. Watching the record turns that into one probe
        interval, and the trial fails with a cause instead of stalling.

        Any other transport failure becomes :class:`SandboxInfraError`. Commands
        go through :mod:`orchard_evalkit.jobs`, which does not re-send one whose
        connection dropped, so the failure reaches here instead — and the
        answer to it is a new trial in a new pod, never a second attempt in
        this one.
        """
        task = asyncio.ensure_future(awaitable)
        try:
            return await self._await_while_alive(task)
        except SandboxInfraError:
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise self._sandbox_gone() from exc
            if is_infra_failure(exc):
                raise self._infra_error(exc) from exc
            raise

    async def _await_while_alive(self, task: asyncio.Task):
        interval = self._settings.liveness_interval
        if interval <= 0 or self._instance is None:
            return await task
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if done:
                    return await task
                if not await self._sandbox_exists():
                    raise self._sandbox_gone()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _sandbox_exists(self, instance=None) -> bool:
        """Whether the orchestrator still has a record for this pod.

        Only a definitive 404 counts as gone. A probe that times out or cannot
        connect says nothing about the pod, and reading it as death would
        abandon healthy trials every time the orchestrator is briefly busy.
        """
        instance, client = instance or self._instance, self._client
        if instance is None or client is None:
            return True
        try:
            await asyncio.wait_for(
                client.get_sandbox(instance.sandbox_id),
                timeout=LIVENESS_PROBE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - only a 404 is conclusive
            if getattr(exc, "status", None) == 404:
                return False
            self.logger.debug("%s: liveness probe failed: %s", self.session_id, exc)
        return True

    def _sandbox_gone(self, instance=None) -> SandboxGone:
        sandbox_id = getattr(instance or self._instance, "sandbox_id", "?")
        return SandboxGone(
            f"the pod for {self.session_id} (sandbox {sandbox_id}) no longer "
            f"exists on {self._settings.base_url}. The orchestrator drops a "
            "sandbox record once its pod disappears, so it was evicted, "
            "OOM-killed or reaped mid-trial. A trial outliving SANDBOX_TTL_HOURS "
            "on the orchestrator ends this way every time; GET /resources shows "
            "whether its sandbox pool is oversubscribed instead. Lower "
            "--concurrency or ORCHARD_HARBOR_DEFAULT_MEMORY if it is."
        )

    def _infra_error(self, cause: BaseException | str) -> SandboxInfraError:
        sandbox_id = getattr(self._instance, "sandbox_id", "?")
        detail = (
            cause if isinstance(cause, str) else f"{type(cause).__name__}: {cause}"
        )
        return SandboxInfraError(
            f"the sandbox for {self.session_id} (sandbox {sandbox_id}) failed "
            f"under the trial: {detail}. Nothing is retried in this pod; with "
            "--infra-retries the trial runs again from scratch in a new one."
        )

    def _pin_endpoint(self) -> None:
        """Choose the one model endpoint this trial's agent may talk to.

        Harbor runs every trial in one process, so a fleet cannot be addressed
        by a process-wide environment variable. It can be addressed here,
        because the agent runs *in the pod* and this provider owns the pod's
        environment.
        """
        key = self._sticky_key()
        self._endpoint = self._settings.endpoint_for(key)
        self._resolve_endpoint_host()
        # Session routing has exactly one base URL, so the fleet test alone
        # would silence this line precisely when it becomes the only per-trial
        # record of which session id the router will see.
        if self._endpoint and (
            len(self._settings.model_base_urls) > 1
            or self._settings.routing == "session"
        ):
            self.logger.info("pinned %s to %s", self.session_id, self._endpoint)

    def _resolve_endpoint_host(self) -> None:
        """Replace the endpoint's hostname with its address, once.

        An isolated pod has no DNS: the allowlist is expressed in CIDRs and the
        orchestrator adds no port-53 exemption, so a hostname fails on the first
        lookup and the failure reads as a model error.

        This has to happen *before* :meth:`_egress_allow` runs, and exactly
        once, because both the allowlist and the agent's base URL are derived
        from ``self._endpoint``. Resolving separately would let a round-robin
        name allowlist one address while the agent dials another.

        Skipped when nothing is ever isolated, so a load-balanced name keeps its
        full address set on runs that do not need the substitution.
        """
        if not self._endpoint or not self._settings.resolve_endpoint:
            return
        if self._settings.allow_internet:
            return
        resolved = with_resolved_host(self._endpoint)
        if resolved != self._endpoint:
            self.logger.debug(
                "%s endpoint %s -> %s (isolated pods have no DNS)",
                self.session_id,
                self._endpoint,
                resolved,
            )
            self._endpoint = resolved

    def _sticky_key(self) -> str:
        """What the endpoint choice hashes: the task, not the attempt.

        A retry keyed by trial id would land on a cold server and re-prefill the
        conversation it is retrying.
        """
        for attribute in ("task_name", "task_id"):
            value = getattr(self, attribute, None)
            if isinstance(value, str) and value:
                return value
        # environment/ lives inside the task directory, so its parent names it.
        environment_dir = getattr(self, "environment_dir", None)
        parent = Path(environment_dir).parent.name if environment_dir else ""
        return parent or str(self.session_id)

    async def _stop_agent_cli(self) -> None:
        """Best-effort: end any payload CLI still running in the pod.

        Dispatched as its own task and deliberately not awaited. The caller is
        already being cancelled, so anything awaited inline would be cancelled
        with it — and the caller has nothing to do with the result anyway. The
        reference is kept only so the task is not garbage-collected mid-flight.

        Matches on the payload directory rather than on CLI names: every payload
        CLI execs from under it, and nothing a task image ships does, so this
        cannot take a build or a test process with it. An agent installed by
        Harbor's own installer does not match — this is a backstop for the
        shim's deadline, not a replacement for it.
        """
        instance = self._instance
        if instance is None:
            return
        # As the user that owns the process: Harbor runs the agent through
        # `exec` with no user of its own, so it lands on the same default, and
        # a signal from anyone else would be refused.
        user = self.default_user
        if user is None:
            user = self._state.user
        command = wrap_user(
            f"pkill -TERM -f {shlex.quote(PAYLOAD_DIR)} 2>/dev/null; "
            f"sleep {AGENT_KILL_GRACE_S}; "
            f"pkill -KILL -f {shlex.quote(PAYLOAD_DIR)} 2>/dev/null; "
            "exit 0",
            _as_user(user),
        )

        async def stop() -> None:
            try:
                await instance.exec(command, timeout=AGENT_KILL_GRACE_S * 3)
            except Exception as exc:  # noqa: BLE001 - never mask the cancellation
                self.logger.debug(
                    "%s: could not stop the agent CLI: %s", self.session_id, exc
                )
            else:
                self.logger.info(
                    "%s: stopped the agent CLI after Harbor stopped waiting on it",
                    self.session_id,
                )

        task = asyncio.ensure_future(stop())
        self._stop_tasks.add(task)
        task.add_done_callback(self._stop_tasks.discard)

    def _agent_deadline_sec(self) -> int | None:
        """Seconds the in-pod agent CLI gets, logged once per trial.

        Logged rather than silent because it is the difference between a
        timeout that returns a transcript and one that leaves an agent running
        unsupervised for another hour, and nothing else in the trial log says
        which of the two a run is configured for.
        """
        if self._deadline_resolved:
            return self._deadline_sec
        self._deadline_resolved = True
        # `--agent-timeout` replaces the task's own budget, exactly as it does
        # in Harbor's `_compute_agent_timeout_sec`. Reading the task file in
        # spite of it would derive the in-pod deadline from a number the run
        # overrode — and an override upwards would then stop every rollout
        # early, which is worse than the orphan this is preventing.
        # Falsy rather than None, because Harbor selects the base with
        # `override_timeout_sec or task.config.agent.timeout_sec` — a zero
        # override falls back to the task there, so it has to here too.
        timeout_sec = self._settings.agent_timeout_override
        if not timeout_sec:
            timeout_sec = task_agent_timeout_sec(
                getattr(self, "environment_dir", None)
            )
        self._deadline_sec = agent_deadline(
            timeout_sec,
            multiplier=self._settings.agent_timeout_multiplier,
            margin=self._settings.agent_deadline_margin,
            exec_timeout=self._settings.exec_timeout,
        )
        if self._deadline_sec is None:
            self.logger.info(
                "%s: no in-pod agent deadline (task timeout_sec=%s, margin=%ss) — "
                "a rollout that spends its whole budget keeps running after "
                "Harbor stops waiting on it",
                self.session_id,
                timeout_sec,
                self._settings.agent_deadline_margin,
            )
        else:
            self.logger.info(
                "%s: in-pod agent deadline %ss (task %ss x %s, less %ss margin)",
                self.session_id,
                self._deadline_sec,
                timeout_sec,
                self._settings.agent_timeout_multiplier,
                self._settings.agent_deadline_margin,
            )
        return self._deadline_sec

    def _endpoint_env(self) -> dict[str, str]:
        if not self._endpoint:
            return {}
        env = dict.fromkeys(BASE_URL_ENV_VARS, self._endpoint)
        # LiteLLM fetches its model-cost map from raw.githubusercontent.com the
        # first time it is imported. An isolated pod reaches only the pinned
        # endpoint, DNS included, so that lookup blocks until it gives up —
        # measured at 38-57 minutes on DeepSWE 1.1, before mini-swe-agent's
        # first model call, in 11 of the 13 rollouts that survived to be read.
        # It is charged to the agent's budget and buys nothing: the fallback is
        # the copy bundled with the package, which is what an air-gapped run
        # was always going to use. Set unconditionally rather than only when
        # isolated, so a networked run resolves prices from the same table and
        # the two stay comparable. pi has its own switch, PI_OFFLINE, below.
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        # Claude Code is the one CLI here that does not speak OpenAI
        # chat-completions: it posts to {ANTHROPIC_BASE_URL}/v1/messages, so it
        # wants the root rather than the /v1 the OpenAI clients above append
        # their protocol path to. Pointed at .../v1 it requests /v1/v1/messages
        # and 404s on turn one. Set unconditionally, like the vars above: an
        # agent that does not read it is not harmed by it.
        env["ANTHROPIC_BASE_URL"] = anthropic_base_url(self._endpoint)
        if self._settings.stage_agent_config:
            env["CODEX_HOME"] = CODEX_HOME
            env["PI_CODING_AGENT_DIR"] = PI_HOME
            env["PI_OFFLINE"] = "1"
        if self._settings.model_api_key:
            env["OPENAI_API_KEY"] = self._settings.model_api_key
            # Harbor's ClaudeCode re-exports whichever key it resolved as
            # ANTHROPIC_API_KEY, which Claude Code sends as the `X-Api-Key`
            # header — one sglang's auth middleware does not read, so every
            # request 401s. ANTHROPIC_AUTH_TOKEN is the spelling that becomes
            # `Authorization: Bearer`. Blanking the other is what makes this the
            # credential that actually goes out: this mapping is merged last
            # into every exec, so an empty value here outranks Harbor's.
            env["ANTHROPIC_AUTH_TOKEN"] = self._settings.model_api_key
            env["ANTHROPIC_API_KEY"] = ""
        # Deliberately no ANTHROPIC_MODEL or ANTHROPIC_DEFAULT_*_MODEL here,
        # and no CLAUDE_CONFIG_DIR: Harbor's own agent sets all of those, and
        # points CLAUDE_CONFIG_DIR at the directory it later reads the native
        # trajectory back out of. Because this mapping merges last, setting one
        # would silently win — and in that case cost every trajectory.
        # Deliberately no GIT_AUTHOR_*/GIT_COMMITTER_* here. Exporting an
        # identity would make every agent's `git commit` work without it having
        # to run `git config` first — but this mapping is merged last into
        # *every* exec, verifier runs included, and those variables outrank
        # `git config user.name` at every level. Terminal-Bench has tasks whose
        # own setup and tests configure an identity and commit with it
        # (`git-multibranch` sets 'Main Dev' inside its test), and overriding
        # those would change a benchmark this fix has no business touching.
        # The pre-collect commit passes its identity with `git -c` instead,
        # where it cannot leak. Agents that hit "Author identity unknown" set
        # one themselves; in one 113-task run all nine that did went on to
        # produce a patch, so this was never the blocker.
        return env

    async def _stage_agent_config(self) -> None:
        """Write the provider config an in-pod agent CLI needs.

        ``OPENAI_BASE_URL`` alone does not reach a self-hosted server: codex
        honours it only for its built-in provider, which talks to
        api.openai.com regardless, and pi rejects any ``--model`` absent from
        its own catalog. Both facts live in a config file, and without it every
        trial dies on turn 1 with an exit code that looks like an agent failure.

        Written for every trial regardless of which agent Harbor will run,
        because the environment is created before the agent is.
        """
        if not self._endpoint or not self._settings.stage_agent_config:
            return
        instance = self._require_instance()
        await instance.exec(f"mkdir -p {CODEX_HOME} {PI_HOME}", timeout=60)
        await instance.upload_content(
            codex_config(self._endpoint, wire_api=self._settings.wire_api).encode(),
            CODEX_CONFIG_PATH,
        )
        if self._settings.model_name:
            await instance.upload_content(
                pi_config(
                    self._endpoint,
                    self._settings.model_name,
                    max_tokens=self._settings.pi_max_tokens,
                ).encode(),
                PI_CONFIG_PATH,
            )
        self.logger.info("staged agent config -> %s", self._endpoint)

    async def _prefer_ipv4(self) -> None:
        """Point ``localhost`` at 127.0.0.1 rather than ``::1``.

        A benchmark's own test suite regularly starts a server and then talks
        to it over ``http://localhost:<port>``. Bind the IPv4 wildcard, as
        NodeBB does — "listening on 0.0.0.0:4568" — and that server does not
        exist on ``::1``. Upstream never meets this: Node asks getaddrinfo with
        ``AI_ADDRCONFIG``, and a single-stack container has no global IPv6
        address for glibc to keep ``::1`` on the strength of. A dual-stack
        Kubernetes pod does, RFC 3484 sorts it first, and the connection is
        refused before the server is consulted.

        Best-effort by construction: this runs once per pod, writes at most two
        files, and a failure leaves the resolver as it was rather than failing
        a trial. See ``OrchardSettings.prefer_ipv4`` for the measurement.
        """
        if not self._settings.prefer_ipv4:
            return
        try:
            result = await self._require_instance().exec(
                PREFER_IPV4_ROUTINE, timeout=PREFER_IPV4_TIMEOUT_SEC
            )
        except Exception as exc:  # noqa: BLE001 - never fail a trial over this
            self.logger.debug(
                "%s: could not prefer IPv4 for localhost: %s", self.session_id, exc
            )
            return
        detail = (result.stdout or "").strip()
        self.logger.info("%s: localhost -> IPv4 (%s)", self.session_id, detail or "?")

    async def _prebuilt_workdir(self, image: str) -> str:
        """Where commands run in a prebuilt image.

        A task's solution is usually written against the image's ``WORKDIR``
        (``cat data.txt``, not ``cat /app/data.txt``), and Orchard pins the pod's
        working directory to ``/workspace`` regardless of what the image says \u2014
        so running there makes the reference solution fail as if the model had.
        """
        workdir = fetch_image_workdir(image)
        if workdir:
            return workdir

        # The registry could not be read: private, rate-limited, or offline.
        # Probe rather than assume, and say so, because a wrong guess here is
        # silent and scores every task in the image zero.
        probe = await self._require_instance().exec("test -d /app", timeout=30)
        fallback = "/app" if probe.exit_code == 0 else "/workspace"
        self.logger.warning(
            "could not read WORKDIR from the config of %s; falling back to %s. "
            "If the task's solution expects another directory it will fail.",
            image,
            fallback,
        )
        return fallback

    async def _connect(self) -> None:
        from orchard_env import AsyncSandboxClient

        self._client = AsyncSandboxClient(
            base_url=self._settings.require_base_url(),
            api_key=self._settings.api_key,
            prefix=self._settings.prefix,
            # A single request must be able to outlive the longest build step
            # or agent loop, since exec waits server-side rather than polling.
            timeout=self._settings.request_timeout,
            # Harbor owns the environment lifecycle; a second cleanup path at
            # interpreter exit would delete pods a resumed job still needs.
            auto_cleanup=False,
        )
        await self._client.__aenter__()
        self._owns_client = True

    def _job_client(self) -> JobClient:
        if self._jobs is None:
            self._jobs = JobClient(
                self._settings.require_base_url(), api_key=self._settings.api_key
            )
        return self._jobs

    def _make_builder(self) -> SandboxBuilder:
        return SandboxBuilder(
            create_sandbox=self._create_sandbox,
            delete_sandbox=self._delete_sandbox,
            image_mirror=self._settings.image_mirror,
            image_remap=dict(self._settings.image_remap),
            chunk_size=self._settings.chunk_size,
            step_timeout=self._settings.step_timeout,
            log=self.logger,
        )

    @override
    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        """Switch the running pod's egress, as Harbor moves between phases.

        Called by ``BaseEnvironment.set_network_policy`` around ``agent.run()``
        and ``verify()``, and again to restore the baseline afterwards.
        """
        blocked = self._blocks_network(network_policy)
        allow = self._egress_allow() if blocked else []
        if (blocked, allow) == self._pod_network:
            # Harbor switches policy per phase, but two different modes can map
            # to the same pod state — every mode does when
            # ORCHARD_HARBOR_ALLOW_INTERNET is set. Skipping keeps that case
            # off an orchestrator that has no /network route at all.
            self.logger.debug(
                "%s already has the requested egress; no change", self.session_id
            )
            return

        # Timed, and the elapsed printed even when it is trivial. Harbor's
        # trial.log carries no timestamps of its own, so the only way to tell
        # where a phase's minutes went is for each step to say how long it
        # took. This one sits between the start of the agent phase and the
        # agent process, which is the window under investigation.
        started = time.monotonic()
        await self._switch_network(self._require_instance(), blocked, allow)
        elapsed = time.monotonic() - started
        self._pod_network = (blocked, allow)
        self.logger.info(
            "%s network policy -> %s in %.1fs (egress allowed to %s)",
            self.session_id,
            getattr(network_policy.network_mode, "value", network_policy.network_mode),
            elapsed,
            self._describe_rules(allow) or ("nothing" if blocked else "anywhere"),
        )

    async def _switch_network(self, instance, blocked: bool, allow: list[dict]) -> None:
        """Apply an egress policy through whichever API the SDK offers.

        The orchestrator has carried two shapes: ``disable_network`` /
        ``enable_network`` taking per-port rules, and before it an older
        ``set_network`` taking bare CIDRs. Dispatching here rather than pinning
        one keeps this provider working across the change instead of across a
        flag day.
        """
        try:
            if hasattr(instance, "disable_network"):
                if blocked:
                    await instance.disable_network(allowlist=allow)
                else:
                    await instance.enable_network()
            elif hasattr(instance, "set_network"):
                await instance.set_network(
                    block_network=blocked, egress_allow=cidrs_of_rules(allow)
                )
            else:
                raise ConfigurationError(
                    f"{self.session_id}: this orchestrator has no network-control "
                    "API, so a task's network_mode cannot be honoured. Deploy one "
                    "that serves PUT /sandboxes/{id}/network, or set "
                    "ORCHARD_HARBOR_ALLOW_INTERNET=1 to run without isolation and "
                    "without the switch."
                )
        except ConfigurationError:
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 404 and not await self._sandbox_exists(
                instance
            ):
                # A 404 here is ambiguous: no /network route on an old
                # orchestrator, or a route that cannot find *this sandbox*. The
                # record settles it, and the difference matters because Harbor
                # restores the baseline policy from the __aexit__ of its phase
                # context — so a pod reaped mid-agent-run always passes through
                # here on the way out. Reported as the route being missing, that
                # 404 replaces the SandboxGone that caused it, and every trial
                # outliving SANDBOX_TTL_HOURS is filed as a deployment problem.
                raise self._sandbox_gone(instance) from exc
            raise ConfigurationError(
                f"{self.session_id}: could not switch network policy. This needs "
                "PUT /sandboxes/{id}/network on the orchestrator; an older "
                "deployment answers 404 or 405 and every no-network task fails "
                "here. Redeploy it, or set ORCHARD_HARBOR_ALLOW_INTERNET=1 to run "
                f"without isolation and without the switch. Underlying error: {exc}"
            ) from exc

    @staticmethod
    def _describe_rules(rules: list[dict]) -> str:
        """``cidr:port`` list for a log line."""
        return ", ".join(
            f"{rule['cidr']}:{rule['port_start']}"
            + (
                f"-{rule['port_end']}"
                if rule.get("port_end") != rule["port_start"]
                else ""
            )
            for rule in rules
        )

    async def _create_sandbox(self, image: str):
        if not image:
            raise ConfigurationError(
                f"{self.session_id}: no image to start; the task declares neither "
                "environment/Dockerfile nor [environment].docker_image"
            )
        assert self._client is not None
        isolated = self._isolated
        allow = self._egress_allow() if isolated else []
        if isolated:
            self.logger.info(
                "%s isolated; egress allowed to %s",
                self.session_id,
                self._describe_rules(allow) or "nothing",
            )
        instance = await self._client.create_sandbox(
            image=image,
            block_network=isolated,
            cpu=self._cpu(),
            memory=self._memory(),
            timeout=self._settings.create_timeout,
        )
        # Creation only takes a boolean, so an allowlist is a second step. It
        # has to be: the orchestrator resolves the policy against a running pod,
        # so there is nothing to attach a rule to until the sandbox is ready.
        # The pod starts fully denied and widens, never the other way around.
        if isolated and allow:
            await self._switch_network(instance, True, allow)
        # Stage pods for `COPY --from` come through here too, after the primary
        # one is assigned; only the primary's state is worth remembering.
        if self._instance is None:
            self._pod_network = (isolated, allow)
        return instance

    def _blocks_network(self, network_policy: NetworkPolicy) -> bool:
        """Whether *network_policy* means this pod gets no general egress."""
        if self._settings.force_isolation:
            if not self._warned_isolation:
                self._warned_isolation = True
                # Same reasoning as the allow_internet warning below, in the
                # other direction: the run is not comparable with one that
                # honoured the task, and nothing else records which kind it was.
                self.logger.warning(
                    "%s: ORCHARD_HARBOR_FORCE_ISOLATION is set, so this task's "
                    "declared network access is NOT being honoured and the pod "
                    "is isolated. Anything the task expected to download will "
                    "fail. Do not compare this score with a networked one.",
                    self.session_id,
                )
            return True
        if network_policy.network_mode != NetworkMode.NO_NETWORK:
            return False
        if self._settings.allow_internet:
            if not self._warned_internet:
                self._warned_internet = True
                # A run made this way is not comparable with one that was not,
                # and nothing else records which kind it was.
                self.logger.warning(
                    "%s: ORCHARD_HARBOR_ALLOW_INTERNET is set, so this task's "
                    "network_mode=no-network is NOT being honoured. The agent "
                    "can reach the upstream repository, where the reference "
                    "solution lives. Do not compare this score with an "
                    "air-gapped one.",
                    self.session_id,
                )
            return False
        return True

    @property
    def _isolated(self) -> bool:
        """Whether the pod is isolated at creation, on the task's baseline."""
        return self._blocks_network(self._network_policy)

    def _egress_allow(self) -> list[dict]:
        """Destinations an isolated pod keeps, as orchestrator allowlist rules.

        Both sources are things the *operator* arranged rather than things the
        task asked for: the endpoint this trial was pinned to, and any extra
        hosts named in ``ORCHARD_HARBOR_EGRESS_ALLOW``. A task's own
        ``allowed_hosts`` never reaches here — allowlist mode is declined in
        ``capabilities``.
        """
        wanted: list[str] = list(self._settings.egress_allow)
        if self._endpoint and self._settings.model_egress:
            wanted.append(self._endpoint)
        if not wanted:
            return []

        rules, unresolved = allow_rules_for(
            wanted, default_ports=self._settings.egress_ports
        )
        if unresolved:
            # Named but unreachable is worth saying out loud: the agent will
            # fail on its first call and the cause will look like a model error.
            self.logger.warning(
                "%s: could not resolve %s to an address; an isolated pod will "
                "not reach it",
                self.session_id,
                ", ".join(unresolved),
            )
        return rules

    def _explain_build_failure(self, exc: BuildError) -> None:
        """Name the one build failure an isolated baseline causes and nothing else explains."""
        if not self._isolated:
            return
        if self._settings.force_isolation:
            self.logger.error(
                "%s: the build ran in a pod with no egress because "
                "ORCHARD_HARBOR_FORCE_ISOLATION is set, not because the task "
                "asked for it. This task's Dockerfile downloads something, so it "
                "cannot be replayed air-gapped. Unset the variable, or allowlist "
                "what the build needs with ORCHARD_HARBOR_EGRESS_ALLOW. "
                "Underlying error: %s",
                self.session_id,
                exc,
            )
            return
        self.logger.error(
            "%s: the build ran in a pod with no egress, because this task's "
            "[environment] baseline is network_mode=no-network and Harbor starts "
            "an environment on its baseline. A Dockerfile that downloads "
            "anything cannot be replayed under it. Move the dependency into "
            "[environment].docker_image, or declare the baseline public and let "
            "the [agent] override isolate the run. Underlying error: %s",
            self.session_id,
            exc,
        )

    async def _delete_sandbox(self, instance) -> None:
        try:
            await instance.delete()
        except Exception as exc:  # noqa: BLE001 - a leaked stage pod is reaped by TTL
            self.logger.debug("failed to delete stage pod: %s", exc)

    def _cpu(self) -> str:
        cpus = self.task_env_config.cpus
        return str(cpus) if cpus else self._settings.default_cpu

    def _memory(self) -> str:
        memory_mb = self.task_env_config.memory_mb
        return f"{memory_mb}Mi" if memory_mb else self._settings.default_memory


def _as_user(user: str | int | None) -> str | None:
    if user is None:
        return None
    return str(user)
