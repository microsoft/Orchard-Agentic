"""Contract tests against Harbor's ``BaseEnvironment``.

Skipped when Harbor is not installed, so the rest of the suite stays runnable on
a laptop with nothing but this package.

These exist because of a specific failure mode: Harbor's environment interface
is abstract, and a missing implementation is not a type error, an import error,
or a lint failure. It surfaces as a ``TypeError`` raised by the factory, several
hundred lines into a trial, after a dataset has been downloaded and a job
started. That is an expensive way to discover a one-line omission.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

harbor = pytest.importorskip("harbor", reason="Harbor is not installed")

from harbor.environments.base import BaseEnvironment  # noqa: E402
from harbor.models.task.config import (  # noqa: E402
    NetworkMode,
    NetworkPolicy,
)

from harbor_orchard import environment  # noqa: E402
from harbor_orchard.environment import (  # noqa: E402
    COMMIT_WORKTREE,
    ConfigurationError,
    OrchardEnvironment,
    SandboxGone,
    SandboxInfraError,
)
from harbor_orchard.plan import StageState  # noqa: E402
from harbor_orchard.settings import OrchardSettings  # noqa: E402
from orchard_evalkit.jobs import ExecDispatchError  # noqa: E402


def test_every_abstract_method_is_implemented():
    missing = sorted(OrchardEnvironment.__abstractmethods__)
    assert missing == [], (
        f"OrchardEnvironment does not implement {missing}. Harbor raises this as "
        "a TypeError from the environment factory, mid-trial."
    )


def test_it_is_a_base_environment():
    assert issubclass(OrchardEnvironment, BaseEnvironment)


def test_capabilities_are_declared_honestly():
    # Each False here is what makes Harbor refuse a task this provider cannot
    # actually run, rather than running it in the wrong environment and scoring
    # a model on the result.
    capabilities = OrchardEnvironment.__new__(OrchardEnvironment).capabilities
    assert capabilities.docker_compose is False
    assert capabilities.gpus is False
    assert capabilities.windows is False
    # Orchard cannot bind-mount, so Harbor must download /logs instead.
    assert capabilities.mounted is False
    # Every DeepSWE task is no-network; declaring False refuses all 113.
    assert capabilities.disable_internet is True
    # Allowlist mode names hosts, which a pod-level NetworkPolicy cannot express.
    assert capabilities.network_allowlist is False


def test_phase_scoped_policy_is_supported():
    # DeepSWE leaves [environment] on the default `public` baseline and
    # overrides [agent]/[verifier] to no-network. Harbor rejects that at trial
    # init unless the provider can switch policy after start:
    #   "[agent] agent phase network policy differs from the agent environment
    #    baseline, but this environment cannot change network policy after start"
    capabilities = OrchardEnvironment.__new__(OrchardEnvironment).capabilities
    assert capabilities.dynamic_network_policy is True


def test_the_dynamic_switch_hook_is_implemented():
    # BaseEnvironment._apply_network_policy raises NotImplementedError by
    # default, and Harbor only calls it mid-trial — after a pod exists.
    assert (
        OrchardEnvironment._apply_network_policy
        is not BaseEnvironment._apply_network_policy
    )


def test_type_is_stable():
    # The provider name appears in session ids and in Harbor's error messages.
    assert OrchardEnvironment.type() == "orchard"


class _NotFound(Exception):
    """What aiohttp raises for a sandbox the orchestrator no longer knows."""

    status = 404


class _FakeInstance:
    sandbox_id = "ef0d2529"


class _FakeClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.probes = 0

    async def get_sandbox(self, sandbox_id: str):
        self.probes += 1
        if self.error is not None:
            raise self.error
        return _FakeInstance()


class _Env(OrchardEnvironment):
    # Class attributes so an instance can be built without Harbor's __init__.
    session_id = "caffe-cifar-10__F9NVve4__env"
    logger = logging.getLogger("harbor_orchard.test")


def _environment(client: _FakeClient) -> _Env:
    env = _Env.__new__(_Env)
    env._settings = OrchardSettings(base_url="http://o", liveness_interval=0.01)
    env._instance = _FakeInstance()
    env._client = client
    return env


class TestSandboxLiveness:
    """Nothing fails an in-flight job when the orchestrator reaps its sandbox.

    So a call waiting on a pod that has been evicted, OOM-killed or aged out of
    SANDBOX_TTL_HOURS is answered by nobody, and blocks for the whole exec
    timeout — 7200s on DeepSWE — printing nothing. These pin the behaviour
    that ends it after one probe instead.
    """

    @pytest.mark.asyncio
    async def test_a_reaped_pod_aborts_the_call_in_flight(self):
        client = _FakeClient(error=_NotFound())
        cancelled = asyncio.Event()

        async def never_returns():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(SandboxGone, match="ef0d2529"):
            await _environment(client)._guard(never_returns())

        # Left running, it would hold the sandbox's exec lock behind it.
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_a_failed_probe_does_not_abandon_a_healthy_call(self):
        # A probe that times out says nothing about the pod. Reading it as
        # death would throw away good trials whenever the orchestrator is busy.
        client = _FakeClient(error=ConnectionError("orchestrator busy"))

        async def slow():
            await asyncio.sleep(0.05)
            return "done"

        assert await _environment(client)._guard(slow()) == "done"
        assert client.probes >= 1

    @pytest.mark.asyncio
    async def test_a_404_from_the_call_itself_still_names_the_pod(self):
        async def gone():
            raise _NotFound()

        with pytest.raises(SandboxGone, match="ef0d2529"):
            await _environment(_FakeClient())._guard(gone())

    @pytest.mark.asyncio
    async def test_any_other_failure_is_left_alone(self):
        async def boom():
            raise RuntimeError("tests failed")

        with pytest.raises(RuntimeError, match="tests failed"):
            await _environment(_FakeClient())._guard(boom())


RULES = [
    {"cidr": "10.4.7.9/32", "protocol": "TCP", "port_start": 30021, "port_end": 30021}
]


class _CurrentInstance:
    """An SDK with the per-port allowlist API."""

    sandbox_id = "ef0d2529"

    def __init__(self):
        self.calls = []

    async def disable_network(self, allowlist=None):
        self.calls.append(("disable", allowlist))
        return {"mode": "restricted", "allowlist": allowlist or []}

    async def enable_network(self):
        self.calls.append(("enable", None))
        return {"mode": "enabled", "allowlist": []}


class _LegacyInstance:
    """An SDK from before the allowlist was expressed per port."""

    sandbox_id = "ef0d2529"

    def __init__(self):
        self.calls = []

    async def set_network(self, block_network=True, egress_allow=None):
        self.calls.append(("set", block_network, egress_allow))
        return {"block_network": block_network, "egress_allow": egress_allow or []}


class _NoNetworkApiInstance:
    sandbox_id = "ef0d2529"


class TestNetworkSwitch:
    """The phase switch, which had no coverage at all before.

    Harbor applies ``[agent]``'s policy between ``setup()`` and ``run()``. If
    that silently does nothing, an air-gapped benchmark still runs and still
    scores — against an agent that could reach the reference solution — so a
    regression here is invisible in the results.
    """

    @pytest.mark.asyncio
    async def test_restricting_sends_the_rules(self):
        instance = _CurrentInstance()
        await _environment(_FakeClient())._switch_network(instance, True, RULES)
        assert instance.calls == [("disable", RULES)]

    @pytest.mark.asyncio
    async def test_opening_asks_for_nothing_narrower(self):
        instance = _CurrentInstance()
        await _environment(_FakeClient())._switch_network(instance, False, [])
        assert instance.calls == [("enable", None)]

    @pytest.mark.asyncio
    async def test_an_older_sdk_gets_bare_cidrs(self):
        # It has no notion of ports, so the rules collapse to their destinations
        # rather than the call failing.
        instance = _LegacyInstance()
        await _environment(_FakeClient())._switch_network(instance, True, RULES)
        assert instance.calls == [("set", True, ["10.4.7.9/32"])]

    @pytest.mark.asyncio
    async def test_an_orchestrator_with_no_network_api_says_so(self):
        # Failing loudly matters more than usual here: the quiet alternative is
        # an un-isolated run that still produces a score.
        with pytest.raises(ConfigurationError, match="ORCHARD_HARBOR_ALLOW_INTERNET"):
            await _environment(_FakeClient())._switch_network(
                _NoNetworkApiInstance(), True, RULES
            )

    @pytest.mark.asyncio
    async def test_a_transport_failure_names_the_route(self):
        class _Broken(_CurrentInstance):
            async def disable_network(self, allowlist=None):
                raise RuntimeError("404 Not Found")

        with pytest.raises(ConfigurationError, match="PUT /sandboxes"):
            await _environment(_FakeClient())._switch_network(_Broken(), True, RULES)

    @pytest.mark.asyncio
    async def test_a_404_on_a_live_sandbox_still_names_the_route(self):
        # The record is there, so the route is what is missing. This is the
        # deployment the message describes, and it has to survive the check
        # below or an old orchestrator looks like a reaped pod instead.
        class _NoRoute(_CurrentInstance):
            async def disable_network(self, allowlist=None):
                raise _NotFound()

        client = _FakeClient()
        with pytest.raises(ConfigurationError, match="PUT /sandboxes"):
            await _environment(client)._switch_network(_NoRoute(), True, RULES)
        assert client.probes == 1

    @pytest.mark.asyncio
    async def test_a_404_on_a_reaped_pod_names_the_pod(self):
        # Harbor restores the baseline policy from the __aexit__ of its phase
        # context, so a pod reaped mid-agent-run reaches this call on its way
        # out. Reported as a missing route, that 404 replaces the SandboxGone
        # that caused it and a TTL problem is filed as a deployment one.
        class _Gone(_CurrentInstance):
            async def enable_network(self):
                raise _NotFound()

        with pytest.raises(SandboxGone, match="SANDBOX_TTL_HOURS"):
            await _environment(_FakeClient(error=_NotFound()))._switch_network(
                _Gone(), False, []
            )


def _policy(mode: NetworkMode) -> NetworkPolicy:
    """``NetworkPolicy`` is a pydantic model, so its mode is keyword-only."""
    return NetworkPolicy(network_mode=mode)


class TestForcedIsolation:
    """Measuring a benchmark air-gapped that never asks to be.

    All 731 SWE-bench Pro tasks ship `allow_internet = true`, so the dataset
    makes an air-gapped score impossible to request — while the fix sits in the
    upstream repository the agent can reach. These pin the override that makes
    it requestable, and pin that it stays off unless asked for.
    """

    @staticmethod
    def _env(**settings):
        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o", **settings)
        env._warned_internet = False
        env._warned_isolation = False
        env._endpoint = None
        return env

    def test_a_public_task_is_isolated_when_forced(self):
        env = self._env(force_isolation=True)
        assert env._blocks_network(_policy(NetworkMode.PUBLIC)) is True

    def test_a_public_task_is_left_alone_by_default(self):
        env = self._env()
        assert env._blocks_network(_policy(NetworkMode.PUBLIC)) is False

    def test_a_no_network_task_is_still_honoured(self):
        # The override widens what can be isolated; it must not narrow it.
        assert self._env()._blocks_network(_policy(NetworkMode.NO_NETWORK)) is True

    def test_the_override_is_announced_once(self, caplog):
        # A forced-isolation score is not comparable with a networked one, and
        # the log line is the only record of which kind a run was.
        env = self._env(force_isolation=True)
        with caplog.at_level(logging.WARNING):
            env._blocks_network(_policy(NetworkMode.PUBLIC))
            env._blocks_network(_policy(NetworkMode.PUBLIC))
        warnings = [r for r in caplog.records if "FORCE_ISOLATION" in r.message]
        assert len(warnings) == 1

    def test_contradictory_overrides_are_rejected(self):
        # One opens a pod the task wanted closed, the other closes one the task
        # wanted open. Letting either win silently is the bug.
        with pytest.raises(ConfigurationError, match="opposite things"):
            OrchardSettings(
                base_url="http://o", allow_internet=True, force_isolation=True
            )


class TestPreCollectCommit:
    """DeepSWE 1.1 grades ``git diff <base> HEAD``, so uncommitted work is a 0.

    Its ``[[verifier.collect]]`` hook extracts the agent's work as a patch and a
    separate verifier container applies it to a pristine checkout; the task's
    own ``solve.sh`` says outright that "only committed work is graded". Neither
    pi nor mini-swe-agent commits on its own, and most rollouts are cut off by
    the agent timeout before they would — measured on one 113-task run, 96 of
    113 pi trials and 64 of 113 mini-swe-agent ones produced a zero-byte
    ``model.patch``, and not one of them scored.

    ``service_exec`` is the rendezvous because Harbor reaches it for collect
    hooks and nothing else: it is after the agent and before anything reads what
    the agent left, on the timeout path as well as the clean one.
    """

    @staticmethod
    def _env(**settings):
        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o", **settings)
        env._committed_before_collect = False
        return env

    def test_a_hook_that_diffs_against_head_is_recognized(self):
        env = self._env()
        deepswe_hook = (
            "cd /app && mkdir -p /logs/artifacts && git config --global --add "
            "safe.directory /app && git diff --binary cb1b3b67 HEAD "
            "> /logs/artifacts/model.patch"
        )
        assert env._reads_committed_history(deepswe_hook) is True

    def test_a_hook_that_reads_the_worktree_is_left_alone(self):
        """Committing would be pointless, and any commit is a side effect."""
        env = self._env()
        assert env._reads_committed_history("cp -r /app/out /logs/artifacts") is False
        assert env._reads_committed_history("git status --porcelain") is False

    def test_only_the_first_hook_commits(self):
        """A task with several hooks must not stack a commit under each."""
        env = self._env()
        hook = "git diff base HEAD > /logs/artifacts/model.patch"
        assert env._reads_committed_history(hook) is True
        env._committed_before_collect = True
        assert env._reads_committed_history(hook) is False

    def test_it_can_be_turned_off(self):
        """Off restores Harbor's behaviour: the benchmark asks the agent to commit."""
        env = self._env(commit_before_collect=False)
        assert env._reads_committed_history("git diff base HEAD") is False


class TestCommitScript:
    """The shell the pre-collect commit runs, checked as text.

    It cannot be executed here — it runs in a task container — so these pin the
    properties that decide whether a rollout is gradable at all.
    """

    @staticmethod
    def _script(dirs=("/app",)):
        return COMMIT_WORKTREE.format(
            dirs=" ".join(dirs), name="'orchard-eval'", email="'orchard-eval@local'"
        )

    def test_it_supplies_an_identity(self):
        """Task images rarely set one, and git refuses to commit without it."""
        script = self._script()
        assert "-c user.name='orchard-eval'" in script
        assert "-c user.email='orchard-eval@local'" in script

    def test_it_marks_the_repo_safe_before_using_it(self):
        """The agent may have run as a different user than this exec does."""
        script = self._script()
        assert script.index("safe.directory") < script.index("rev-parse")

    def test_it_skips_a_tree_the_agent_already_committed(self):
        """``git add -A`` stages nothing, so there is nothing to commit."""
        assert "diff --cached --quiet" in self._script()

    def test_it_does_not_run_repository_hooks(self):
        assert "--no-verify" in self._script()

    def test_it_never_reports_failure(self):
        """A trial that cannot commit is no less gradable than before."""
        script = self._script()
        assert script.rstrip().endswith("exit 0")
        assert "|| true" in script

    def test_it_leaves_excludes_to_gitignore(self):
        """An agent's caches and venvs are the repository's business, not ours."""
        assert "add -A" in self._script()
        assert "exclude" not in self._script()


class TestLiteLLMStaysOffline:
    """LiteLLM must not try to fetch its cost map from inside an isolated pod.

    The fetch is made on first import, against raw.githubusercontent.com, and
    an isolated pod reaches only the pinned endpoint — DNS included. The lookup
    blocks until it gives up, and it is the agent's budget that pays: 38-57
    minutes before mini-swe-agent's first model call, on 11 of the 13 DeepSWE
    rollouts that survived long enough to be read. Nothing in the result
    differs, because the fallback is the table bundled with the package.
    """

    @staticmethod
    def _env(**settings):
        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o", **settings)
        env._endpoint = "http://model/v1"
        return env

    def test_the_local_cost_map_is_forced(self):
        assert self._env()._endpoint_env()["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"

    def test_it_does_not_depend_on_staging_agent_config(self):
        # Staging writes codex/pi config files. mini-swe-agent has none, and it
        # is the harness that pays for this fetch.
        exported = self._env(stage_agent_config=False)._endpoint_env()
        assert exported["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


class TestGitIdentityIsNotExported:
    """The identity must not leak into execs other than our own commit.

    ``_endpoint_env`` is merged last into *every* exec, verifier runs included,
    and ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` outrank ``git config user.name`` at
    every level. Terminal-Bench's ``git-multibranch`` configures 'Main Dev' and
    commits with it inside its own test; exporting an identity here would
    silently reauthor that commit. The pre-collect commit uses ``git -c``
    instead, which is scoped to the one command that needs it.
    """

    @staticmethod
    def _env(**settings):
        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o", **settings)
        env._endpoint = "http://model/v1"
        return env

    def test_no_git_variables_reach_the_pod(self):
        exported = self._env()._endpoint_env()
        assert [k for k in exported if k.startswith("GIT_")] == []

    def test_the_identity_still_reaches_our_own_commit(self):
        script = COMMIT_WORKTREE.format(
            dirs="/app", name="'bot'", email="'bot@x'"
        )
        assert "-c user.name='bot'" in script
        assert "-c user.email='bot@x'" in script


class TestClaudeCodeReachesTheFleet:
    """What ``_endpoint_env`` has to say — and has to not say — about Claude Code.

    Harbor's own ``ClaudeCode`` already sets the model aliases, ``IS_SANDBOX``,
    the output cap, and ``CLAUDE_CONFIG_DIR``. This mapping is merged last into
    every exec, so anything named here outranks all of that. Two of those
    overrides are wanted and the rest would be damage.
    """

    @staticmethod
    def _env(**settings):
        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o", **settings)
        env._endpoint = "http://h:30021/session/org%2Ftask/v1"
        return env

    def test_the_endpoint_loses_the_v1_the_cli_re_adds(self):
        exported = self._env()._endpoint_env()
        assert exported["ANTHROPIC_BASE_URL"] == "http://h:30021/session/org%2Ftask"

    def test_the_credential_goes_out_as_a_bearer_token(self):
        exported = self._env(model_api_key="sk-local")._endpoint_env()
        assert exported["ANTHROPIC_AUTH_TOKEN"] == "sk-local"

    def test_harbors_own_x_api_key_is_blanked(self):
        # ClaudeCode._resolve_auth_env() re-exports whatever key it resolved as
        # ANTHROPIC_API_KEY, which the CLI sends as X-Api-Key — a header sglang
        # does not read. Blanking it here is what makes the bearer token above
        # the credential that actually goes out.
        exported = self._env(model_api_key="sk-local")._endpoint_env()
        assert exported["ANTHROPIC_API_KEY"] == ""

    def test_the_trajectory_directory_is_left_to_harbor(self):
        # Harbor points CLAUDE_CONFIG_DIR at <environment_logs_dir>/sessions and
        # reads the native trajectory back out of it. Setting it here would win,
        # and cost every Harbor claude trajectory.
        assert "CLAUDE_CONFIG_DIR" not in self._env()._endpoint_env()

    def test_the_model_aliases_are_left_to_harbor(self):
        # Same merge-order argument: Harbor already mirrors the model into all
        # of these when a base URL is configured, and a value here would silently
        # replace whatever it resolved.
        exported = self._env(model_api_key="sk-local")._endpoint_env()
        assert [k for k in exported if k.startswith("ANTHROPIC_DEFAULT_")] == []
        assert "ANTHROPIC_MODEL" not in exported


class TestInPodAgentDeadline:
    """Harbor's agent deadline does not stop the agent.

    ``asyncio.wait_for`` around the agent coroutine cancels the await and
    leaves the CLI running in the pod, where it blocks log collection and the
    verifier behind it. Measured on a 731-trial SWE-bench Pro run: 34% of
    timed-out trials then sat for a median of 65 minutes, the last model call
    landing a median of 2687s *after* the recorded agent end, and 189 pods aged
    past SANDBOX_TTL_HOURS while blocked and were never graded. Trials whose
    agent exited cleanly never did this — 0 of 209. These pin the deadline that
    is exported into the pod so the CLI stops itself first.
    """

    @staticmethod
    def _env(tmp_path, *, timeout_sec="5400.0", **settings):
        if timeout_sec is not None:
            (tmp_path / "task.toml").write_text(f"[agent]\ntimeout_sec = {timeout_sec}\n")
        environment_dir = tmp_path / "environment"
        environment_dir.mkdir(exist_ok=True)

        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o", **settings)
        env.environment_dir = environment_dir
        env._deadline_sec = None
        env._deadline_resolved = False
        return env

    def test_it_is_harbors_deadline_less_the_margin(self, tmp_path):
        env = self._env(tmp_path, agent_timeout_multiplier=1.5, exec_timeout=9000)
        assert env._agent_deadline_sec() == 7980

    def test_an_override_replaces_the_task_budget(self, tmp_path):
        # Harbor's --agent-timeout wins over the task file, so this has to too:
        # 3600 x 1.5 - 120, not the task's 5400 x 1.5 - 120.
        env = self._env(
            tmp_path,
            agent_timeout_override=3600.0,
            agent_timeout_multiplier=1.5,
            exec_timeout=9000,
        )
        assert env._agent_deadline_sec() == 5280

    def test_an_override_applies_even_with_no_task_file(self, tmp_path):
        env = self._env(
            tmp_path,
            timeout_sec=None,
            agent_timeout_override=3600.0,
            agent_timeout_multiplier=1.0,
            exec_timeout=9000,
        )
        assert env._agent_deadline_sec() == 3480

    def test_a_zero_override_falls_back_to_the_task(self, tmp_path):
        # Harbor picks the base with `override or task_timeout`, so a zero
        # override is no override there. Diverging would compute a deadline
        # from a budget Harbor is not using.
        env = self._env(
            tmp_path,
            agent_timeout_override=0.0,
            agent_timeout_multiplier=1.5,
            exec_timeout=9000,
        )
        assert env._agent_deadline_sec() == 7980

    def test_a_task_without_a_budget_gets_no_deadline(self, tmp_path):
        env = self._env(tmp_path, timeout_sec=None, exec_timeout=9000)
        assert env._agent_deadline_sec() is None

    def test_it_is_resolved_once_and_kept(self, tmp_path):
        # A rollout issues hundreds of execs and the answer cannot change
        # within a trial, so the task.toml is not re-read for each one.
        env = self._env(tmp_path, exec_timeout=9000)
        first = env._agent_deadline_sec()
        (tmp_path / "task.toml").write_text("[agent]\ntimeout_sec = 60.0\n")
        assert env._agent_deadline_sec() == first

    def test_a_resolved_absence_is_also_kept(self, tmp_path):
        # The "not looked up yet" and "looked up, there is none" cases are
        # both None, so a flag rather than the value has to carry the state.
        env = self._env(tmp_path, timeout_sec=None, exec_timeout=9000)
        assert env._agent_deadline_sec() is None
        (tmp_path / "task.toml").write_text("[agent]\ntimeout_sec = 5400.0\n")
        assert env._agent_deadline_sec() is None


class TestSessionClose:
    """Teardown hands this trial's engine back to the session router.

    The router cannot see a rollout end — the last request of a forty-turn
    conversation is indistinguishable from the pause before turn forty-one — so
    without this it waits out an idle window sized for an agent running a test
    suite between turns. That wait is also biased: phantom load accumulates in
    proportion to how fast an engine retires trials, so the fastest engine is
    the one that stops being given work.
    """

    @staticmethod
    def _env(endpoint):
        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o")
        env._instance = None
        env._jobs = None
        env._client = None
        env._owns_client = False
        env._endpoint = endpoint
        return env

    @staticmethod
    def _record(monkeypatch):
        closed = []

        async def fake_close(endpoint, **kwargs):
            closed.append(endpoint)
            return endpoint is not None

        monkeypatch.setattr(environment, "close_session", fake_close)
        return closed

    @pytest.mark.asyncio
    async def test_stopping_closes_the_session(self, monkeypatch):
        closed = self._record(monkeypatch)
        await self._env("http://h:30021/session/write-compressor/v1").stop(delete=True)
        assert closed == ["http://h:30021/session/write-compressor/v1"]

    @pytest.mark.asyncio
    async def test_the_pod_goes_first(self, monkeypatch):
        order = []

        async def fake_close(endpoint, **kwargs):
            order.append("close")
            return True

        class _Deleting:
            sandbox_id = "sbx"

            async def delete(self):
                order.append("delete")

        monkeypatch.setattr(environment, "close_session", fake_close)
        env = self._env("http://h:30021/session/a/v1")
        env._instance = _Deleting()
        await env.stop(delete=True)
        # An undeleted pod stays charged to the run; an unclosed session only
        # holds its engine until the router's idle window expires.
        assert order == ["delete", "close"]

    @pytest.mark.asyncio
    async def test_a_trial_that_never_pinned_an_endpoint_still_stops(
        self, monkeypatch
    ):
        # stop() also runs on the build-failure path, before any model request.
        closed = self._record(monkeypatch)
        await self._env(None).stop(delete=False)
        assert closed == [None]

    @pytest.mark.asyncio
    async def test_a_close_that_raises_anyway_does_not_fail_the_teardown(
        self, monkeypatch
    ):
        # close_session swallows its own failures, so this only fires if that
        # contract breaks — at which point a trial whose result is already
        # decided would start reporting a teardown error.
        deleted = []

        async def angry_close(endpoint, **kwargs):
            raise RuntimeError("the router is on fire")

        class _Deleting:
            sandbox_id = "sbx"

            async def delete(self):
                deleted.append(True)

        monkeypatch.setattr(environment, "close_session", angry_close)
        env = self._env("http://h:30021/session/a/v1")
        env._instance = _Deleting()
        await env.stop(delete=True)
        assert deleted == [True]


class TestPreferIPv4IsOptional:
    """The step itself: off by request, and never fatal."""

    @staticmethod
    def _env(instance, **settings):
        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o", **settings)
        env._instance = instance
        return env

    class _Recording:
        sandbox_id = "sbx"

        def __init__(self, error=None):
            self.commands = []
            self._error = error

        async def exec(self, command, timeout=None):
            self.commands.append(command)
            if self._error is not None:
                raise self._error
            return type("R", (), {"stdout": "hosts gai.conf", "exit_code": 0})()

    @pytest.mark.asyncio
    async def test_it_runs_by_default(self):
        instance = self._Recording()
        await self._env(instance)._prefer_ipv4()
        assert len(instance.commands) == 1
        assert "gai.conf" in instance.commands[0]

    @pytest.mark.asyncio
    async def test_it_can_be_turned_off(self):
        """For a cluster where the pod's resolver is deliberately configured."""
        instance = self._Recording()
        await self._env(instance, prefer_ipv4=False)._prefer_ipv4()
        assert instance.commands == []

    @pytest.mark.asyncio
    async def test_a_failure_does_not_fail_the_trial(self):
        """An image with no awk is still a usable environment for most tasks."""
        instance = self._Recording(error=RuntimeError("no such file"))
        await self._env(instance)._prefer_ipv4()


class TestInfraFailuresEndTheTrial:
    """A sandbox that failed under a trial is never used again.

    Commands used to go through the SDK's ``exec``, which answered a dropped
    ``POST /exec`` by sending the command again — a second agent CLI on the
    tree the first had already edited, in 297 of 613 trials of one SWE-bench
    Pro V2 run. The job client raises instead, and these pin what happens next:
    one exception type Harbor can retry on (``--retry-include
    SandboxInfraError``, which reruns the trial in a brand-new pod), and no
    further command sent to the failed one.
    """

    class _Jobs:
        """Stands in for :class:`orchard_evalkit.jobs.JobClient`."""

        def __init__(self, *outcomes):
            self._outcomes = list(outcomes)
            self.commands = []

        async def run(self, sandbox_id, command, **kwargs):
            self.commands.append(command)
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    @staticmethod
    def _job(exit_code, error=None):
        return SimpleNamespace(exit_code=exit_code, stdout="", stderr="", error=error)

    @staticmethod
    def _env(jobs=None):
        env = _Env.__new__(_Env)
        env._settings = OrchardSettings(base_url="http://o", liveness_interval=0)
        env._instance = SimpleNamespace(sandbox_id="600967f6")
        env._jobs = jobs or TestInfraFailuresEndTheTrial._Jobs()
        env._client = _FakeClient()
        env._state = StageState()
        env._endpoint = None
        env._deadline_resolved = True
        env._deadline_sec = None
        env._failure = None
        env._stop_tasks = set()
        env.default_user = None
        env.task_env_config = SimpleNamespace(workdir="/app")
        env._merge_env = lambda overlay: None
        return env

    def test_the_name_is_the_one_harbor_retries_on(self):
        # Harbor matches --retry-include against type(exc).__name__.
        assert SandboxInfraError.__name__ == "SandboxInfraError"
        assert issubclass(SandboxGone, SandboxInfraError)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            ExecDispatchError("POST failed after it was sent"),
            aiohttp.ServerDisconnectedError(),
            aiohttp.ClientResponseError(MagicMock(), (), status=502),
            TimeoutError("Job j1 did not complete within 3600s"),
        ],
        ids=["dispatch", "disconnect", "502", "client-deadline"],
    )
    async def test_a_transport_failure_becomes_an_infra_error(self, error):
        async def call():
            raise error

        with pytest.raises(SandboxInfraError, match="600967f6"):
            await self._env()._guard(call())

    @pytest.mark.asyncio
    async def test_a_client_error_is_left_alone(self):
        # A 400 fails the same way in any pod; retrying it costs a rollout.
        async def call():
            raise aiohttp.ClientResponseError(MagicMock(), (), status=400)

        with pytest.raises(aiohttp.ClientResponseError):
            await self._env()._guard(call())

    @pytest.mark.asyncio
    async def test_a_failing_command_is_a_result(self):
        env = self._env(self._Jobs(self._job(1)))
        result = await env.exec("pytest")
        assert result.return_code == 1
        assert env._failure is None

    @pytest.mark.asyncio
    async def test_a_command_that_never_reached_the_pod_is_an_infra_error(self):
        env = self._env(self._Jobs(self._job(None, "No pod IP available")))
        with pytest.raises(SandboxInfraError, match="No pod IP available"):
            await env.exec("mini-swe-agent --task ...")

    @pytest.mark.asyncio
    async def test_a_command_timeout_is_still_reported_as_one(self):
        env = self._env(
            self._Jobs(self._job(None, "Execution timed out after 3600s"))
        )
        result = await env.exec("sleep 9999")
        assert result.return_code == -1
        assert env._failure is None

    @pytest.mark.asyncio
    async def test_nothing_more_is_sent_to_a_pod_that_failed(self):
        # Harbor still captures the patch and artifacts from a failed trial;
        # in this pod each would queue behind the orphaned agent.
        jobs = self._Jobs(ExecDispatchError("dropped"), self._job(0))
        env = self._env(jobs)
        with pytest.raises(SandboxInfraError):
            await env.exec("mini-swe-agent --task ...")
        with pytest.raises(SandboxInfraError, match="earlier command already failed"):
            await env.exec("git diff --cached > /logs/agent/model.patch")
        assert len(jobs.commands) == 1

    @pytest.mark.asyncio
    async def test_a_pod_that_cannot_be_created_is_an_infra_error(self):
        env = self._env()
        env._start = AsyncMock(side_effect=aiohttp.ClientConnectionError("refused"))
        with pytest.raises(SandboxInfraError, match="ClientConnectionError"):
            await env.start(force_build=False)

    @pytest.mark.asyncio
    async def test_a_task_the_provider_cannot_run_is_not_retried(self):
        env = self._env()
        env._start = AsyncMock(side_effect=ConfigurationError("needs compose"))
        with pytest.raises(ConfigurationError):
            await env.start(force_build=False)
