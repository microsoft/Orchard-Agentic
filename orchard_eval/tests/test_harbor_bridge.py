"""The Harbor bridge's side of fleet routing.

The provider does the per-trial pinning; this layer only has to hand it the
endpoint list, and hand the host-side fallback the first one.
"""

from __future__ import annotations

import pytest

from orchard_evalkit.cli import build_parser
from orchard_evalkit.harbor_bridge import (
    AGENT_ALIASES,
    INFRA_RETRY_EXCEPTIONS,
    STOCK_AGENTS_ENV,
    HarborRunSpec,
    TrialOutcome,
    resolve_agent,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "MODEL_BASE_URL",
        "MODEL_BASE_URL_REPLICAS",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        STOCK_AGENTS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _endpoints(argv: list[str]) -> list[str]:
    from orchard_evalkit.cli import _harbor_endpoints

    return _harbor_endpoints(build_parser().parse_args(argv))


class TestEndpointResolution:
    def test_replicas_expand_to_consecutive_ports(self):
        endpoints = _endpoints(
            [
                "harbor",
                "-d",
                "terminal-bench/terminal-bench-2-1@latest",
                "--base-url",
                "http://h:30021/v1",
                "--base-url-replicas",
                "8",
            ]
        )
        assert endpoints[0] == "http://h:30021/v1"
        assert endpoints[-1] == "http://h:30028/v1"

    def test_environment_is_the_default(self, monkeypatch):
        monkeypatch.setenv("MODEL_BASE_URL", "http://h:30021/v1")
        monkeypatch.setenv("MODEL_BASE_URL_REPLICAS", "4")
        assert len(_endpoints(["harbor", "-d", "d/s@1"])) == 4

    def test_no_endpoint_configured(self):
        assert _endpoints(["harbor", "-d", "d/s@1"]) == []


class TestSpecEnvironment:
    def test_fleet_is_passed_to_the_provider(self):
        spec = HarborRunSpec(
            dataset="d/s@1",
            base_urls=["http://h:30021/v1", "http://h:30022/v1"],
        )
        env = spec.environment()
        assert env["MODEL_BASE_URLS"] == "http://h:30021/v1,http://h:30022/v1"
        assert env["MODEL_ROUTING"] == "sticky"
        # Host-side agents cannot be pinned per trial, so they get the first.
        assert env["OPENAI_BASE_URL"] == "http://h:30021/v1"

    def test_session_routing_reaches_the_provider(self):
        # The provider is what inserts the path segment, so all this side has
        # to do is name the mode and the router's one endpoint.
        spec = HarborRunSpec(
            dataset="d/s@1", base_urls=["http://h:30021/v1"], routing="session"
        )
        env = spec.environment()
        assert env["MODEL_ROUTING"] == "session"
        assert env["MODEL_BASE_URLS"] == "http://h:30021/v1"

    def test_an_explicit_openai_base_url_is_left_alone(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://proxy:9000/v1")
        spec = HarborRunSpec(dataset="d/s@1", base_urls=["http://h:30021/v1"])
        assert "OPENAI_BASE_URL" not in spec.environment()

    def test_nothing_is_overridden_without_a_fleet(self):
        assert HarborRunSpec(dataset="d/s@1").environment() == {}

    def test_claude_effort_is_pinned_for_this_fleet(self):
        # Harbor's ClaudeCode turns this into `--effort`, through
        # CliFlag.env_fallback — the harbor process reads it, not the pod, so
        # harbor_orchard.environment cannot be where it is set. Left alone,
        # Claude Code asks for effort "high", which a Qwen chat template
        # rejects with an uncaught jinja error: HTTP 500 on turn one of every
        # trial.
        spec = HarborRunSpec(dataset="d/s@1", base_urls=["http://h:30021/v1"])
        assert spec.environment()["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"

    def test_an_explicit_claude_effort_is_left_alone(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
        spec = HarborRunSpec(dataset="d/s@1", base_urls=["http://h:30021/v1"])
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in spec.environment()

    def test_claude_effort_is_not_pinned_against_anthropic(self):
        # No base_urls means the run is not pointed at this fleet, and the
        # template that rejects "high" is this fleet's.
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in HarborRunSpec(
            dataset="d/s@1"
        ).environment()


class TestAgentTimeoutMultiplier:
    """``--agent-timeout-multiplier`` is a Harbor flag the provider never sees.

    It needs it: Harbor's agent deadline is ``task timeout_sec x multiplier``,
    and the provider stops the in-pod CLI just under that so the CLI is not
    left running after Harbor stops waiting on it. Exported from the same argv
    the flag is passed in, so the two cannot drift.
    """

    def _env(self, *extra_args):
        return HarborRunSpec(dataset="d/s@1", extra_args=list(extra_args)).environment()

    def test_it_is_exported_for_the_provider(self):
        env = self._env("--agent-timeout-multiplier", "1.5")
        assert env["ORCHARD_HARBOR_AGENT_MULTIPLIER"] == "1.5"

    def test_the_equals_spelling_works_too(self):
        # This reads a passthrough list, not a parsed namespace, so both
        # spellings argparse accepts have to be handled here.
        env = self._env("--agent-timeout-multiplier=2")
        assert env["ORCHARD_HARBOR_AGENT_MULTIPLIER"] == "2"

    def test_a_run_without_the_flag_exports_nothing(self):
        # The provider defaults to 1.0, which under-estimates the deadline and
        # therefore only ever stops the agent early.
        assert "ORCHARD_HARBOR_AGENT_MULTIPLIER" not in self._env("--n-attempts", "2")

    def test_a_trailing_flag_with_no_value_is_ignored(self):
        assert "ORCHARD_HARBOR_AGENT_MULTIPLIER" not in self._env(
            "--agent-timeout-multiplier"
        )

    def test_the_agent_timeout_override_is_exported_too(self):
        # --agent-timeout replaces the task's own budget before the multiplier
        # is applied. The provider derives its in-pod deadline from the task
        # file, so missing this would stop every rollout early.
        env = self._env("--agent-timeout", "3600")
        assert env["ORCHARD_HARBOR_AGENT_TIMEOUT_SEC"] == "3600"

    def test_the_two_timeout_flags_do_not_collide(self):
        # "--agent-timeout" is a prefix of "--agent-timeout-multiplier".
        env = self._env("--agent-timeout-multiplier", "1.5")
        assert "ORCHARD_HARBOR_AGENT_TIMEOUT_SEC" not in env
        env = self._env("--agent-timeout-multiplier=1.5")
        assert "ORCHARD_HARBOR_AGENT_TIMEOUT_SEC" not in env

    def test_both_are_exported_together(self):
        env = self._env("--agent-timeout", "3600", "--agent-timeout-multiplier", "1.5")
        assert env["ORCHARD_HARBOR_AGENT_TIMEOUT_SEC"] == "3600"
        assert env["ORCHARD_HARBOR_AGENT_MULTIPLIER"] == "1.5"

    def test_the_generic_multiplier_is_the_fallback(self):
        # Harbor's _resolve_timeout_sec falls back to --timeout-multiplier when
        # no agent-specific one is given. Reading only the specific flag would
        # leave the provider on 1.0 and halve every in-pod deadline.
        env = self._env("--timeout-multiplier", "2")
        assert env["ORCHARD_HARBOR_AGENT_MULTIPLIER"] == "2"

    def test_the_specific_multiplier_wins_over_the_generic(self):
        env = self._env("--timeout-multiplier", "2", "--agent-timeout-multiplier", "1.5")
        assert env["ORCHARD_HARBOR_AGENT_MULTIPLIER"] == "1.5"

    def test_it_survives_alongside_a_fleet(self):
        spec = HarborRunSpec(
            dataset="d/s@1",
            base_urls=["http://h:30021/v1"],
            extra_args=["--agent-timeout-multiplier", "1.5"],
        )
        env = spec.environment()
        assert env["ORCHARD_HARBOR_AGENT_MULTIPLIER"] == "1.5"
        assert env["MODEL_BASE_URLS"] == "http://h:30021/v1"


class TestFailureCategory:
    def test_an_agent_exit_is_not_a_build_failure(self):
        # An agent's command line routinely contains the word "build", which
        # used to send the reader to the translation layer instead of agent/.
        outcome = TrialOutcome(
            task="kv-store-grpc",
            trial="kv-store-grpc__czbKUui",
            reward=0.0,
            error="NonZeroAgentExitCodeError: Command failed (exit 1): codex exec --build",
        )
        assert outcome.failure_category == "AGENT_ERROR"

    def test_a_real_build_failure_still_reports_one(self):
        outcome = TrialOutcome(
            task="t", trial="t__1", reward=None, error="BuildError: step 4/9 failed"
        )
        assert outcome.failure_category == "BUILD_FAILED"

    def test_the_in_pod_deadline_is_a_timeout_not_an_agent_error(self):
        # `timeout` exits 124, so Harbor raises NonZeroAgentExitCodeError and
        # the AGENT_ERROR rule would claim it — filing a budget that is too
        # small under a category that means "the CLI is broken".
        outcome = TrialOutcome(
            task="t",
            trial="t__1",
            reward=0.0,
            error=(
                "NonZeroAgentExitCodeError: Command failed (exit 124): "
                "mini-swe-agent --yolo\nstderr: orchard: the agent CLI was "
                "stopped at its in-pod deadline of 2880s"
            ),
        )
        assert outcome.failure_category == "TIMEOUT"


    def test_a_sandbox_that_failed_every_attempt_is_infra(self):
        # The cause quotes a transport error, which says "timed out" often
        # enough that the TIMEOUT rule would otherwise claim it.
        outcome = TrialOutcome(
            task="t",
            trial="t__1",
            reward=None,
            error=(
                "SandboxInfraError: the sandbox for t__1__env (sandbox 600967f6) "
                "failed under the trial: JobWaitError: lost the orchestrator for "
                "300s: ServerTimeoutError: timed out"
            ),
        )
        assert outcome.failure_category == "SANDBOX_INFRA"


class TestInfraRetries:
    """A trial whose sandbox failed is rerun in a new pod, never in the old one.

    The rerun is Harbor's own: ``--max-retries`` creates the trial again, which
    starts a new environment, and ``--retry-include`` limits that to the
    exceptions that mean the sandbox failed.
    """

    @staticmethod
    def _retry_flags(argv):
        includes = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--retry-include"]
        retries = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--max-retries"]
        return retries, includes

    def test_infra_failures_are_retried_by_default(self):
        retries, includes = self._retry_flags(HarborRunSpec(dataset="d/s@1").command())
        assert retries == ["2"]
        assert includes == list(INFRA_RETRY_EXCEPTIONS)

    def test_only_infra_failures_are_named(self):
        # Anything named here is rerun; an agent timeout or an agent error is a
        # result, and rerunning it would turn pass@1 into best-of-n.
        for name in ("AgentTimeoutError", "NonZeroAgentExitCodeError"):
            assert name not in INFRA_RETRY_EXCEPTIONS
        assert "SandboxInfraError" in INFRA_RETRY_EXCEPTIONS

    def test_zero_turns_it_off(self):
        argv = HarborRunSpec(dataset="d/s@1", infra_retries=0).command()
        assert self._retry_flags(argv) == ([], [])

    def test_a_policy_passed_through_is_left_whole(self):
        argv = HarborRunSpec(
            dataset="d/s@1", extra_args=["--max-retries", "5"]
        ).command()
        assert self._retry_flags(argv) == (["5"], [])

    def test_the_short_spelling_counts_as_a_policy_too(self):
        argv = HarborRunSpec(dataset="d/s@1", extra_args=["-r", "1"]).command()
        assert "--max-retries" not in argv

    def test_the_cli_flag_reaches_the_spec(self):
        args = build_parser().parse_args(["harbor", "-d", "d/s@1", "--infra-retries", "1"])
        assert args.infra_retries == 1
        assert build_parser().parse_args(["harbor", "-d", "d/s@1"]).infra_retries == 2


class TestAgentSubstitution:
    """Which agent class Harbor is actually told to load.

    Harbor's own `pi` and `mini-swe-agent` fetch their CLI at trial time, into
    the task's image. On SWE-bench Pro that lost 88 trials each — nvm has no
    musl Node build, and uv resolves mini's dependencies against whatever Python
    the image happens to carry. The pod already has both CLIs mounted, so the
    short name a caller types selects the subclass that uses them.
    """

    def test_pi_resolves_to_ours(self):
        assert resolve_agent("pi") == AGENT_ALIASES["pi"]
        assert HarborRunSpec(dataset="d/s@1", agent="pi").command().count(
            "harbor_orchard.agents:Pi"
        ) == 1

    def test_mini_resolves_to_ours(self):
        assert resolve_agent("mini-swe-agent") == AGENT_ALIASES["mini-swe-agent"]

    def test_codex_resolves_to_ours_for_the_patch_capture(self):
        # Harbor's codex agent needs no install help — it short-circuits when
        # codex is present and the payload's build is musl. The alias exists so
        # codex also writes the model.patch a fresh-sandbox re-grade replays;
        # without it every codex trial re-grades as zero.
        assert resolve_agent("codex") == AGENT_ALIASES["codex"]

    def test_agents_harbor_handles_correctly_are_left_alone(self):
        assert resolve_agent("oracle") == "oracle"
        assert resolve_agent("nop") == "nop"

    def test_an_explicit_import_path_wins(self):
        assert resolve_agent("my.module:Agent") == "my.module:Agent"

    def test_the_stock_agents_can_be_asked_for(self, monkeypatch):
        monkeypatch.setenv(STOCK_AGENTS_ENV, "1")
        assert resolve_agent("pi") == "pi"

    def test_a_falsy_opt_out_still_substitutes(self, monkeypatch):
        monkeypatch.setenv(STOCK_AGENTS_ENV, "0")
        assert resolve_agent("pi") == AGENT_ALIASES["pi"]


class TestTaskFiles:
    """A subset worth re-running is derived, not typed.

    88 repeated `--task` flags is not a usable way to hand back the list that
    `scripts/harbor_failed_tasks.py` just printed.
    """

    def _spec_args(self, argv: list[str]) -> list[str]:
        from orchard_evalkit.cli import _read_task_files

        args = build_parser().parse_args(argv)
        return args.task + _read_task_files(args.task_file)

    def test_names_are_read_from_the_file(self, tmp_path):
        listing = tmp_path / "tasks.txt"
        listing.write_text(
            "# the musl subset\n"
            "\n"
            "scale-ai/instance_protonmail__webclients-01ea5214\n"
            "scale-ai/instance_gravitational__teleport-005dcb16  # heaviest\n",
            encoding="utf-8",
        )
        assert self._spec_args(
            ["harbor", "-d", "d/s@1", "--task-file", str(listing)]
        ) == [
            "scale-ai/instance_protonmail__webclients-01ea5214",
            "scale-ai/instance_gravitational__teleport-005dcb16",
        ]

    def test_a_file_combines_with_explicit_tasks(self, tmp_path):
        listing = tmp_path / "tasks.txt"
        listing.write_text("b\n", encoding="utf-8")
        assert self._spec_args(
            ["harbor", "-d", "d/s@1", "--task", "a", "--task-file", str(listing)]
        ) == ["a", "b"]

    def test_a_missing_file_stops_the_run(self, tmp_path):
        with pytest.raises(SystemExit, match="task-file"):
            self._spec_args(
                ["harbor", "-d", "d/s@1", "--task-file", str(tmp_path / "nope.txt")]
            )


class TestCheckedInTaskLists:
    def test_the_musl_list_is_the_88_instances_it_claims(self):
        from pathlib import Path

        from orchard_evalkit.cli import _read_task_files

        listing = (
            Path(__file__).resolve().parents[1] / "configs" / "swebench-pro-musl.txt"
        )
        names = _read_task_files([str(listing)])
        # A silently shortened list would make a retry look clean while leaving
        # tasks unrun, so the count is asserted, not just the format.
        assert len(names) == 88
        assert len(set(names)) == 88
        assert all(name.startswith("scale-ai/instance_") for name in names)


def _write_trial(parent, name, reward=1.0, **extra):
    import json

    trial = parent / name
    (trial / "agent" / "logs").mkdir(parents=True)
    # A nested file of the same name that is not a trial must not be counted.
    (trial / "agent" / "logs" / "result.json").write_text("{}")
    data = {
        "task_name": f"bench/{name}",
        "trial_name": name,
        "verifier_result": {"rewards": {"reward": reward}},
        **extra,
    }
    (trial / "result.json").write_text(json.dumps(data))


class TestCollectOutcomes:
    def test_reads_trials_directly_under_the_attempt(self, tmp_path):
        from orchard_evalkit.harbor_bridge import collect_outcomes

        (tmp_path / "result.json").write_text('{"id": "job-level"}')
        _write_trial(tmp_path, "b__2", reward=0.0)
        _write_trial(tmp_path, "a__1")
        # A trial still running has its directory but no result.json yet.
        (tmp_path / "c__3").mkdir()

        outcomes = collect_outcomes(tmp_path)
        assert [o.trial for o in outcomes] == ["a__1", "b__2"]
        assert [o.solved for o in outcomes] == [True, False]

    def test_falls_back_to_the_whole_tree_for_a_job_directory(self, tmp_path):
        from orchard_evalkit.harbor_bridge import collect_outcomes

        # A job directory holding attempts: depth 1 is the job-level result.
        attempt = tmp_path / "20260925-000000"
        attempt.mkdir()
        (attempt / "result.json").write_text('{"id": "job-level"}')
        _write_trial(attempt, "a__1")

        assert [o.trial for o in collect_outcomes(tmp_path)] == ["a__1"]

    def test_keeps_the_trial_timestamps(self, tmp_path):
        from orchard_evalkit.harbor_bridge import collect_outcomes

        _write_trial(
            tmp_path,
            "a__1",
            started_at="2026-09-24T08:27:44.581369Z",
            finished_at="2026-09-24T08:53:48.898211Z",
        )
        _write_trial(tmp_path, "b__2", started_at="not a date")

        a, b = collect_outcomes(tmp_path)
        assert (a.finished_at - a.started_at).total_seconds() == pytest.approx(1564.3, abs=0.1)
        assert b.started_at is None and b.finished_at is None

    def test_keeps_the_agents_tokens_and_its_directory(self, tmp_path):
        from orchard_evalkit.harbor_bridge import collect_outcomes

        _write_trial(
            tmp_path,
            "a__1",
            agent_result={"n_input_tokens": 9198733, "n_output_tokens": 76127},
        )
        # A patch replay keeps no accounting of its own.
        _write_trial(
            tmp_path,
            "b__2",
            agent_result={"n_input_tokens": None, "n_output_tokens": None},
        )

        a, b = collect_outcomes(tmp_path)
        assert (a.input_tokens, a.output_tokens) == (9198733, 76127)
        assert (b.input_tokens, b.output_tokens) == (None, None)
        assert a.trial_dir == tmp_path / "a__1"
