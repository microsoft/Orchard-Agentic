"""Drive a Harbor job from ``orchard-eval``.

This is deliberately a bridge and not a second implementation. Harbor already
owns the parts of a Harbor run that are easy to get subtly wrong — dataset
resolution from the Hub, the oracle agent, the ``/tests`` upload, reward-file
parsing, separate verifier environments, artifact transfer between them — and a
reimplementation would drift from upstream every time terminal-bench ships a
release. What this repository owns instead is the *environment*: the
``harbor_orchard`` provider that turns a task's Dockerfile into commands and its
mounts into transfers.

So the bridge does three things: assemble the ``harbor run`` invocation with the
Orchard provider selected, run it, and re-read the trial results into the same
shape the rest of ``orchard-eval`` reports in.
"""

from __future__ import annotations

import json
import re
import logging
import os
import shutil
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# One source of truth for the level: the Harbor arm and the native arm run the
# same CLI against the same fleet, and a difference between them would show up
# as a score gap rather than as an error.
from .harnesses.installed_cli import DEFAULT_CLAUDE_EFFORT_FALLBACK

logger = logging.getLogger(__name__)

#: How Harbor loads the provider. The ``:``-separated form is what
#: ``--env`` recognises as a custom environment rather than a built-in one.
ENVIRONMENT_IMPORT_PATH = "harbor_orchard:OrchardEnvironment"

#: Replays each task's own ``solution/solve.sh``. A dataset's oracle score is
#: the ceiling on every model score measured against it afterwards.
ORACLE_AGENT = "oracle"

#: Agents this repository substitutes for Harbor's own. Harbor's ``--agent``
#: accepts an ``module:Class`` import path wherever it accepts a name, so the
#: short name a caller types still selects the right class.
#:
#: ``pi`` and ``mini-swe-agent`` are here for one reason: Harbor's versions
#: fetch their CLI from the internet at trial time, into the task's image, and
#: Orchard pods already carry a prebuilt one. See :mod:`harbor_orchard.agents`
#: for what that costs on SWE-bench Pro — 88 lost trials per agent.
#:
#: ``claude-code`` is here for a narrower reason. Harbor's version already skips
#: its install when the CLI exists, but decides that with ``command -v`` — which
#: cannot tell a glibc payload on a musl host from a working one.
#:
#: ``codex`` is here for a third reason, and not for either of those: its
#: install needs no help at all (it short-circuits the same way, and its payload
#: build is static-PIE musl, so the musl question never arises). It is aliased
#: only so it inherits ``harbor_orchard.agents._ModelPatchCapture`` and writes
#: the ``model.patch`` that a fresh-sandbox re-grade replays. Nothing else about
#: the agent changes, and ``ORCHARD_HARBOR_STOCK_AGENTS=1`` opts out.
AGENT_ALIASES = {
    "pi": "harbor_orchard.agents:Pi",
    "mini-swe-agent": "harbor_orchard.agents:MiniSweAgent",
    "claude-code": "harbor_orchard.agents:ClaudeCode",
    "codex": "harbor_orchard.agents:Codex",
}

#: Opt back out to Harbor's stock agents, for comparing against upstream
#: behaviour or when the payload is not mounted.
STOCK_AGENTS_ENV = "ORCHARD_HARBOR_STOCK_AGENTS"

#: How long to let ``harbor run`` tear its trials down after a Ctrl-C. Long
#: enough to delete a few hundred pods, short enough not to look hung.
SHUTDOWN_GRACE_S = 120

#: Exceptions that mean the sandbox failed under a trial, not the agent in it:
#: ``SandboxInfraError`` and its subclass ``SandboxGone`` from
#: ``harbor_orchard.environment``, and Harbor's own for a pod that never came
#: up. Harbor retries a trial that ends in one of these by creating it again —
#: a new pod, a clean checkout, a full agent budget — and deletes the failed
#: attempt's directory. Names, not classes: ``--retry-include`` compares
#: ``type(exc).__name__``, and Harbor's default exclusions (``AgentTimeoutError``
#: and the like) stay in force, so a rollout that ran out of budget is a result.
INFRA_RETRY_EXCEPTIONS = (
    "SandboxInfraError",
    "SandboxGone",
    "EnvironmentStartTimeoutError",
)

#: Fresh-pod reruns per trial. Matches ``rollout_retries`` on the native path.
DEFAULT_INFRA_RETRIES = 2

#: Reasoning effort for Harbor's ``claude-code``, which reaches its ``--effort``
#: flag through ``CliFlag.env_fallback`` — so this is read by the *harbor*
#: process, not by anything in the pod, and cannot be set from
#: ``harbor_orchard.environment`` with the rest of the agent's variables.
#:
#: Needed for the same reason the native harness pins it: Claude Code sends
#: ``output_config.effort`` on every request and defaults it to ``high``, sglang
#: forwards that as chat-completions ``reasoning_effort``, and a Qwen chat
#: template raises on any level but ``xhigh``/``medium``/``low`` — an uncaught
#: jinja error that arrives as HTTP 500 on turn one of every trial. Under
#: ``--routing session`` the level is then discarded anyway, because the router
#: drops the field so the template applies its own ``xhigh`` default; this is
#: the fallback for the paths where it does not. Only set when there are
#: ``base_urls``, i.e. when the run points at this fleet rather than at
#: Anthropic. An explicit export, or ``--ae CLAUDE_CODE_EFFORT_LEVEL=``, or
#: ``-c agent.reasoning_effort=`` all still outrank it.
CLAUDE_EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"


def resolve_agent(agent: str) -> str:
    """The ``--agent`` value to hand Harbor, substituting our agents for its.

    An explicit import path — anything already containing ``:`` — is passed
    through untouched, so the aliases never get in the way of naming a class
    directly.
    """
    stock = os.environ.get(STOCK_AGENTS_ENV, "").strip().lower()
    if ":" in agent or (stock and stock not in ("0", "false", "no")):
        return agent
    return AGENT_ALIASES.get(agent, agent)


class HarborNotInstalled(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "The `harbor` CLI is not on PATH. From the repository root:\n"
            "  python -m pip install harbor\n"
            "  python -m pip install -e orchard_env -e orchard_eval/harbor_orchard\n"
            "harbor_orchard must be importable by the harbor process itself, so "
            "both have to end up in the same environment."
        )


@dataclass
class HarborRunSpec:
    """Everything that varies between Harbor runs."""

    #: ``org/name@version`` for a Hub dataset, mutually exclusive with ``path``.
    dataset: str | None = None
    #: A local dataset or task directory.
    path: str | None = None
    agent: str = ORACLE_AGENT
    model: str | None = None
    jobs_dir: Path = Path("./results/harbor")
    job_name: str = ""
    n_concurrent: int = 8
    n_attempts: int = 1
    #: Reruns, in a new pod, of a trial whose sandbox failed under it. See
    #: :data:`INFRA_RETRY_EXCEPTIONS`; ``0`` leaves Harbor's retries off.
    infra_retries: int = DEFAULT_INFRA_RETRIES
    #: ``--include-task-name``; repeatable upstream, so a list here.
    include_tasks: list[str] = field(default_factory=list)
    exclude_tasks: list[str] = field(default_factory=list)
    n_tasks: int | None = None
    agent_kwargs: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)
    #: Interchangeable endpoints serving the same model. Several of them pin
    #: each trial to one, for prefix-cache reuse across an agent's turns.
    base_urls: list[str] = field(default_factory=list)
    routing: str = "sticky"
    wire_api: str = "responses"

    def environment(self) -> dict[str, str]:
        """Environment overrides for the ``harbor`` process.

        ``MODEL_BASE_URLS`` is read by ``harbor_orchard``, which pins each trial
        to one of them inside the pod. The ``OPENAI_*`` pair is the fallback for
        an agent whose model loop runs on the host instead, and gets the first
        endpoint — such an agent cannot be pinned per trial from here.

        ``CLAUDE_CODE_EFFORT_LEVEL`` is read by Harbor itself rather than by
        anything in the pod — see :data:`CLAUDE_EFFORT_ENV`.
        """
        env: dict[str, str] = {}
        multiplier = self.agent_timeout_multiplier()
        if multiplier is not None:
            # harbor_orchard needs Harbor's agent deadline to stop the in-pod
            # CLI just before it, and the multiplier that produces it is a
            # Harbor CLI flag the provider never sees. Exported from the same
            # argv it is parsed out of, so the two cannot drift.
            env["ORCHARD_HARBOR_AGENT_MULTIPLIER"] = multiplier
        timeout_sec = self.agent_timeout_sec()
        if timeout_sec is not None:
            # Same reason, for the other half of Harbor's arithmetic.
            env["ORCHARD_HARBOR_AGENT_TIMEOUT_SEC"] = timeout_sec
        if not self.base_urls:
            return env
        env |= {
            "MODEL_BASE_URLS": ",".join(self.base_urls),
            "MODEL_ROUTING": self.routing,
            "MODEL_WIRE_API": self.wire_api,
        }
        for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
            if not os.environ.get(name):
                env[name] = self.base_urls[0]
        if not os.environ.get(CLAUDE_EFFORT_ENV):
            env[CLAUDE_EFFORT_ENV] = DEFAULT_CLAUDE_EFFORT_FALLBACK
        return env

    def agent_timeout_multiplier(self) -> str | None:
        """The multiplier Harbor will actually apply to the agent phase.

        Harbor falls back to the generic ``--timeout-multiplier`` when no agent
        -specific one is given (``Trial._resolve_timeout_sec``), so reading only
        the specific flag would leave the provider on 1.0 while Harbor used 2 —
        and an in-pod deadline derived from 1.0 would then stop every rollout
        at half its budget.
        """
        specific = self._passthrough("--agent-timeout-multiplier")
        if specific is not None:
            return specific
        return self._passthrough("--timeout-multiplier")

    def agent_timeout_sec(self) -> str | None:
        """``--agent-timeout`` as it was passed through, if it was.

        Harbor's ``--agent-timeout`` replaces the task's own
        ``[agent] timeout_sec`` before the multiplier is applied. The provider
        derives its in-pod deadline from the task file, so without this it
        would keep deriving it from a budget the run overrode — and an override
        *upwards* would make the in-pod deadline fire early and cut rollouts
        short, which is worse than the orphan it exists to prevent.
        """
        return self._passthrough("--agent-timeout")

    def _passthrough(self, flag: str) -> str | None:
        """*flag*'s value out of ``extra_args``.

        Both spellings argparse accepts, since this reads a passthrough list
        rather than a parsed namespace.
        """
        for index, arg in enumerate(self.extra_args):
            if arg == flag and index + 1 < len(self.extra_args):
                return self.extra_args[index + 1]
            if arg.startswith(f"{flag}="):
                return arg.split("=", 1)[1]
        return None

    def command(self) -> list[str]:
        if bool(self.dataset) == bool(self.path):
            raise ValueError("exactly one of dataset= or path= must be set")

        argv = ["harbor", "run", "--env", ENVIRONMENT_IMPORT_PATH]
        if self.dataset:
            argv += ["--dataset", self.dataset]
        else:
            argv += ["--path", str(self.path)]

        argv += ["--agent", resolve_agent(self.agent)]
        if self.model:
            argv += ["--model", self.model]
        argv += ["--jobs-dir", str(self.jobs_dir)]
        if self.job_name:
            argv += ["--job-name", self.job_name]
        argv += ["--n-concurrent", str(self.n_concurrent)]
        if self.n_attempts != 1:
            argv += ["--n-attempts", str(self.n_attempts)]
        # A retry policy passed through by hand is the caller's whole policy,
        # so ours is not mixed into it.
        caller_set_retries = any(
            self._passthrough(flag) is not None for flag in ("--max-retries", "-r")
        )
        if self.infra_retries > 0 and not caller_set_retries:
            argv += ["--max-retries", str(self.infra_retries)]
            for name in INFRA_RETRY_EXCEPTIONS:
                argv += ["--retry-include", name]
        if self.n_tasks is not None:
            argv += ["--n-tasks", str(self.n_tasks)]
        for name in self.include_tasks:
            argv += ["--include-task-name", name]
        for name in self.exclude_tasks:
            argv += ["--exclude-task-name", name]
        for key, value in self.agent_kwargs.items():
            argv += ["--ak", f"{key}={value}"]
        argv += self.extra_args
        return argv


@dataclass
class TrialOutcome:
    task: str
    trial: str
    reward: float | None
    error: str | None = None
    #: From ``agent/exit-code.txt``, which Harbor writes only on a non-zero exit.
    agent_exit_code: int | None = None
    #: Every metric the verifier wrote, keys lowercased. DeepSWE reports F2P/P2P
    #: test counts beside the scalar, which Harbor prints as a histogram with the
    #: metric name dropped.
    rewards: dict = field(default_factory=dict)
    #: The trial's own ``started_at`` / ``finished_at``, when Harbor recorded them.
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: The agent's prompt and completion tokens, summed over its LLM calls, from
    #: ``agent_result``. Prompt tokens count every call's whole context again,
    #: cached or not, so they grow with the square of the turn count.
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: The directory the trial's ``result.json`` was read from.
    trial_dir: Path | None = None

    @property
    def solved(self) -> bool:
        return self.reward is not None and self.reward > 0

    @property
    def test_counts(self) -> tuple[int, int, int, int] | None:
        """``(f2p_passed, f2p_total, p2p_passed, p2p_total)``, when reported."""
        keys = ("f2p_passed", "f2p_total", "p2p_passed", "p2p_total")
        if not all(key in self.rewards for key in keys):
            return None
        try:
            return tuple(int(self.rewards[key]) for key in keys)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None

    @property
    def failure_category(self) -> str:
        """Group failures by what actually went wrong.

        An oracle run that reports 60% is useless without this: the difference
        between "the translation layer could not build the image" and "the
        solution ran but the verifier disagreed" is the difference between a bug
        here and a bug in the task.
        """
        if self.solved:
            return "SOLVED"
        if self.error is None:
            return "REWARD_ZERO"
        lowered = self.error.lower()
        # Before the AGENT_ERROR rule, which would otherwise claim this: the
        # in-pod deadline stops the CLI with `timeout`, so Harbor sees a
        # non-zero exit and raises NonZeroAgentExitCodeError. The rollout did
        # not fail, it ran out of budget — and unlike a bare timeout it came
        # back with its transcript and diff intact.
        if "in-pod deadline" in lowered:
            return "TIMEOUT"
        # The sandbox failed under every attempt Harbor was allowed to make.
        # Early, because the cause it quotes is a transport error and may well
        # say "timed out" or name the network.
        if "sandboxinfraerror" in lowered:
            return "SANDBOX_INFRA"
        # Checked before the substring rules below: an agent's own command line
        # routinely contains the word "build", which read as a build failure.
        if "nonzeroagentexitcode" in lowered or "agent exited" in lowered:
            return "AGENT_ERROR"
        if "dockerfile" in lowered or "builderror" in lowered or "build step" in lowered:
            return "BUILD_FAILED"
        if "docker-compose" in lowered or "several networked" in lowered:
            return "UNSUPPORTED_COMPOSE"
        # Before the network rule: a pod reaped mid-trial fails whatever call
        # comes next, and when that call is Harbor restoring the phase's network
        # policy the trial is reported as a network error. It is not one — the
        # cluster took the environment away — and reading it as one sends the
        # next run at the orchestrator's /network route instead of at its TTL.
        if "sandboxgone" in lowered or "no longer exists" in lowered:
            return "SANDBOX_GONE"
        if "no-network" in lowered or "network_mode" in lowered:
            return "UNSUPPORTED_NETWORK"
        if "gpu" in lowered:
            return "UNSUPPORTED_GPU"
        if "timeout" in lowered or "timed out" in lowered:
            return "TIMEOUT"
        return "TRIAL_ERROR"


def run(spec: HarborRunSpec, *, env: dict[str, str] | None = None) -> int:
    """Execute ``harbor run`` and stream its output. Returns the exit code."""
    if shutil.which("harbor") is None:
        raise HarborNotInstalled()

    spec.jobs_dir.mkdir(parents=True, exist_ok=True)
    argv = spec.command()
    logger.info("running: %s", " ".join(argv))
    process = subprocess.Popen(argv, env={**os.environ, **(env or {})})
    try:
        return process.wait()
    except KeyboardInterrupt:
        return _shut_down(process)


def _shut_down(process: subprocess.Popen) -> int:
    """Let harbor finish deleting its sandboxes before this process exits.

    Ctrl-C already reached harbor, which shares this process group. What it
    does not survive is this process returning immediately: the terminal comes
    back, the run looks over, and every pod harbor had not yet torn down keeps
    running an agent against the model server until the orchestrator's TTL
    reaps it hours later.
    """
    logger.warning(
        "interrupted; giving harbor up to %ds to stop its trials and delete "
        "their sandboxes. Ctrl-C again to skip that.",
        SHUTDOWN_GRACE_S,
    )
    try:
        return process.wait(timeout=SHUTDOWN_GRACE_S)
    except subprocess.TimeoutExpired:
        logger.warning("harbor is still shutting down; terminating it")
    except KeyboardInterrupt:
        logger.warning("skipping graceful shutdown; terminating harbor")

    process.terminate()
    try:
        return process.wait(timeout=30)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        return process.wait()


#: One attempt of a Harbor job, as run_all_evals.sh names them.
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}$")


def newest_attempt(job_dir: Path) -> Path:
    """The attempt to read, given a Harbor job directory.

    Re-running a config writes a new ``YYYYmmdd-HHMMSS`` subdirectory rather
    than writing over the last one, mirroring ``<run-name>/<run-id>/`` on the
    native path. Reading the parent instead would fold every attempt together,
    so one is picked and the rest are left on disk.

    The newest that *recorded something* wins, not simply the newest — the same
    rule ``config.latest_run_id`` applies. An attempt that died before its first
    trial leaves an empty directory, and preferring it would hide the last real
    score behind a crash, dropping the job from the scoreboard entirely. Harbor
    writes a trial's ``result.json`` even when the trial raised, so its presence
    is what "recorded something" means.

    When nothing has recorded yet the newest is returned anyway, so a job that
    has only just started reads as running rather than as missing.

    A job directory holding trials directly, from before attempts were nested,
    is returned unchanged.
    """
    try:
        attempts = [
            d for d in job_dir.iterdir() if d.is_dir() and RUN_ID_RE.match(d.name)
        ]
    except OSError:
        return job_dir
    if not attempts:
        return job_dir
    recorded = [d for d in attempts if any(d.rglob("result.json"))]
    return max(recorded or attempts, key=lambda d: d.name)


def job_dirs(jobs_dir: Path) -> set[Path]:
    try:
        return {path for path in jobs_dir.iterdir() if path.is_dir()}
    except OSError:
        return set()


def resolve_job_dir(jobs_dir: Path, before: set[Path]) -> Path:
    """The directory Harbor wrote *this* run's trials into.

    Harbor names job directories by timestamp (or ``--job-name``) under a shared
    ``--jobs-dir``, so the tree accumulates every previous run. Summarizing the
    whole tree silently averages this run together with its own history, which
    is worse than reporting nothing. Diffing the directory listing across the
    run identifies the new one without depending on the naming scheme.
    """
    current = job_dirs(jobs_dir)
    candidates = (current - before) or current
    if not candidates:
        return jobs_dir
    return max(candidates, key=lambda path: path.stat().st_mtime)


def collect_outcomes(job_dir: Path) -> list[TrialOutcome]:
    """Read every trial result Harbor wrote under *job_dir*.

    The file is ``result.json``, singular — ``TrialPaths.result_path``. Harbor's
    own docstring in ``models/trial/paths.py`` says ``results.json``, which is
    stale.

    One ``result.json`` also sits at the job root with a different schema; the
    ``task_name`` check below is what separates the two. Harbor writes a trial's
    file even when the trial raised, so a failed run still reports its cause.

    Trials sit directly under an attempt, so that one level is read first and
    in parallel; on NFS a recursive walk through every trial's trajectories and
    logs costs far more than the reads themselves. Only when no trial turns up
    there — *job_dir* is a job holding attempts, or a whole ``--jobs-dir`` — does
    this fall back to searching the entire tree.
    """
    try:
        shallow = sorted(
            Path(entry.path) / "result.json"
            for entry in os.scandir(job_dir)
            if entry.is_dir()
        )
    except OSError:
        shallow = []
    outcomes = _read_outcomes(shallow, missing_ok=True)
    if not outcomes:
        outcomes = _read_outcomes(sorted(job_dir.rglob("result.json")))
    return outcomes


#: Reads are latency-bound on a network filesystem, not CPU-bound.
_READ_WORKERS = 32


def _read_outcomes(paths: list[Path], missing_ok: bool = False) -> list[TrialOutcome]:
    if not paths:
        return []
    with ThreadPoolExecutor(max_workers=min(_READ_WORKERS, len(paths))) as pool:
        return [
            outcome
            for outcome in pool.map(lambda p: _read_outcome(p, missing_ok), paths)
            if outcome is not None
        ]


def _read_outcome(results_path: Path, missing_ok: bool) -> TrialOutcome | None:
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not missing_ok:
            logger.warning("skipping vanished %s", results_path)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("skipping unreadable %s: %s", results_path, exc)
        return None
    if not isinstance(data, dict) or "task_name" not in data:
        return None  # the job-level result, not a trial
    return _to_outcome(data, results_path)


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_outcome(data: dict, results_path: Path) -> TrialOutcome:
    verifier = data.get("verifier_result") or {}
    rewards = {
        str(key).lower(): value
        for key, value in (verifier.get("rewards") or {}).items()
    }
    reward = _primary_reward(rewards)
    exit_code = _agent_exit_code(results_path.parent)
    input_tokens, output_tokens = agent_tokens(data)

    exception = data.get("exception_info") or {}
    error = None
    if exception:
        error = f"{exception.get('exception_type')}: {exception.get('exception_message')}"
    elif reward is None:
        error = "verifier produced no reward file"
    elif exit_code is not None:
        error = f"solution exited {exit_code} — see agent/oracle.txt"

    return TrialOutcome(
        task=data.get("task_name", results_path.parent.name),
        trial=data.get("trial_name", results_path.parent.name),
        reward=reward,
        error=error,
        agent_exit_code=exit_code,
        rewards=rewards,
        started_at=_timestamp(data.get("started_at")),
        finished_at=_timestamp(data.get("finished_at")),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        trial_dir=results_path.parent,
    )


def agent_tokens(data: dict) -> tuple[int | None, int | None]:
    """``(input, output)`` tokens out of a trial ``result.json``, when recorded.

    Harbor fills ``agent_result`` from the agent's own accounting; an agent
    that keeps none, like the patch replay of a re-grade, leaves both None.
    """
    agent = data.get("agent_result")
    if not isinstance(agent, dict):
        return None, None

    def count(key: str) -> int | None:
        value = agent.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    return count("n_input_tokens"), count("n_output_tokens")


def _agent_exit_code(trial_dir: Path) -> int | None:
    """Whether the agent's own script failed, and how.

    A zero-reward trial is ambiguous on its own: the solution may have crashed,
    been killed, or run clean and simply not satisfied the tests. This file is
    written only for a non-zero exit, so its presence alone narrows that down.
    """
    try:
        return int((trial_dir / "agent" / "exit-code.txt").read_text().strip())
    except (OSError, ValueError):
        return None


def _primary_reward(rewards: dict) -> float | None:
    """Pick the scalar a task is scored on.

    ``reward.json`` may carry several metrics. Harbor scores on ``reward`` when
    present; otherwise a single-metric file is unambiguous, and anything else is
    left unscored rather than guessed at.
    """
    if not rewards:
        return None
    if "reward" in rewards:
        return float(rewards["reward"])
    if len(rewards) == 1:
        return float(next(iter(rewards.values())))
    return None


def summarize(outcomes: Iterable[TrialOutcome]) -> dict:
    outcomes = list(outcomes)
    solved = [outcome for outcome in outcomes if outcome.solved]

    # Keyed by category, valued by the trials in it: a count alone says a sweep
    # fell short without saying what to open next.
    failed: dict[str, list[TrialOutcome]] = {}
    for outcome in outcomes:
        if not outcome.solved:
            failed.setdefault(outcome.failure_category, []).append(outcome)

    ordered = dict(sorted(failed.items(), key=lambda item: -len(item[1])))
    return {
        "total": len(outcomes),
        "solved": len(solved),
        "solve_rate": len(solved) / len(outcomes) if outcomes else 0.0,
        "failures": {name: len(items) for name, items in ordered.items()},
        "failed_trials": {
            name: [(item.task, item.trial, item.error) for item in items]
            for name, items in ordered.items()
        },
        "tests": [
            (item.task, item.reward, *counts)
            for item in outcomes
            if (counts := item.test_counts) is not None
        ],
    }


#: What a category means, so a shortfall names its own suspect.
_CATEGORY_HELP = {
    "BUILD_FAILED": "Dockerfile replay failed — a translation-layer bug",
    "AGENT_ERROR": "the agent CLI exited non-zero — see <trial>/agent/",
    "UNSUPPORTED_COMPOSE": "multi-container task; one sandbox cannot serve it",
    "UNSUPPORTED_NETWORK": "task requires no-network; egress is fixed at create",
    "SANDBOX_GONE": "the pod was reaped mid-trial — usually SANDBOX_TTL_HOURS",
    "SANDBOX_INFRA": "the sandbox or orchestrator failed on every attempt — see --infra-retries",
    "UNSUPPORTED_GPU": "task requires a GPU",
    "TIMEOUT": "agent or verifier ran out of time",
    "REWARD_ZERO": "solution ran but the verifier scored it zero",
    "TRIAL_ERROR": "the trial raised — see the trial.log",
}


def format_summary(summary: dict, *, label: str) -> str:
    lines = [
        "",
        f"  {label:<24} {summary['solved']}/{summary['total']} = "
        f"{summary['solve_rate']:.2%}",
        "",
    ]
    failed_trials = summary.get("failed_trials", {})
    for category, count in summary["failures"].items():
        help_text = _CATEGORY_HELP.get(category, "")
        lines.append(f"  {count:>4}  {category:<22} {help_text}")
        for task, trial, error in failed_trials.get(category, []):
            lines.append(f"          {task}  ({trial})")
            if error:
                lines.append(f"            {error.splitlines()[0][:110]}")
    lines += _format_tests(summary.get("tests") or [])
    return "\n".join(lines) + "\n"


def _format_tests(tests: list) -> list[str]:
    """One row per trial: the tests behind the score.

    Reward is all-or-nothing — every F2P test green and no P2P regression — so a
    zero says nothing about whether the agent was close or did nothing at all.
    """
    if not tests:
        return []
    width = max(len(str(task)) for task, *_ in tests)
    lines = [
        "",
        f"  {'task':<{width}}  {'F2P':>11}  {'P2P':>13}  reward",
    ]
    for task, reward, f2p_passed, f2p_total, p2p_passed, p2p_total in tests:
        score = "-" if reward is None else f"{reward:g}"
        lines.append(
            f"  {task:<{width}}  {f'{f2p_passed}/{f2p_total}':>11}  "
            f"{f'{p2p_passed}/{p2p_total}':>13}  {score:>6}"
        )
    lines.append(
        "  F2P: the tests the fix must turn green. P2P: regressions, green already."
    )
    return lines


def write_summary(job_dir: Path, summary: dict, *, label: str) -> Path:
    """Persist the summary beside Harbor's own artifacts.

    Harbor's ``result.json`` holds the raw rewards; this is the same run read in
    this suite's terms — solve rate, failure categories, and the F2P/P2P counts
    behind each score — so a finished job can be re-read without re-deriving it
    from 113 trial directories.
    """
    payload = {"label": label, **summary}
    json_path = job_dir / "orchard-summary.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (job_dir / "orchard-summary.txt").write_text(
        format_summary(summary, label=label), encoding="utf-8"
    )
    return json_path
