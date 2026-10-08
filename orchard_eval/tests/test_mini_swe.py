"""mini-swe-agent as an in-sandbox CLI harness.

These tests pin the details that make ``mini`` work unattended on a pod and
that a casual edit would silently break: the non-interactive agent class, the
local (not docker) environment, the first-run wizard being disabled, and the
trajectory being read from a file rather than from stdout.
"""

import json

import pytest

from orchard_evalkit.config import ModelConfig, RunConfig
from orchard_evalkit.harnesses.base import RolloutContext
from orchard_evalkit.harnesses.installed_cli import PROMPT_PATH
from orchard_evalkit.harnesses.mini_swe import (
    MINI_HOME,
    MINI_INSTALL_COMMAND,
    MINI_TRAJECTORY_PATH,
    MINI_VERSION_SPEC,
    MiniSweAgentHarness,
)
from orchard_evalkit.harnesses.trajectory import parse_mini_trajectory
from orchard_evalkit.models import EXIT_AGENT_ERROR, EXIT_COMPLETED, TaskInstance
from orchard_evalkit.sandbox import EvalSandbox
from tests.fakes import FakeJobResult, FakeSandboxInstance

SUBMITTED_PATCH = "diff --git a/f b/f\n+fixed\n"

TRAJECTORY = json.dumps(
    {
        "trajectory_format": "mini-swe-agent-1.1",
        "info": {
            "mini_version": "2.4.6",
            "exit_status": "Submitted",
            "submission": SUBMITTED_PATCH,
            "model_stats": {"instance_cost": 0.42, "api_calls": 7},
        },
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "<pr_description>...</pr_description>"},
            {"role": "assistant", "content": "Looking around.", "extra": {}},
            {"role": "tool", "content": "<returncode>0</returncode>"},
            {"role": "exit", "content": SUBMITTED_PATCH},
        ],
    }
)


def _context(
    responder=None, workdir: str = "/testbed"
) -> tuple[RolloutContext, FakeSandboxInstance]:
    instance = FakeSandboxInstance(responder=responder)
    instance.files[MINI_TRAJECTORY_PATH] = TRAJECTORY.encode()
    ctx = RolloutContext(
        sandbox=EvalSandbox(instance, workdir=workdir),
        instance=TaskInstance(
            instance_id="astropy__astropy-12907",
            problem_statement="Fix the thing",
            base_commit="abc123",
            workdir=workdir,
        ),
        config=RunConfig(),
    )
    return ctx, instance


def _harness(**params) -> MiniSweAgentHarness:
    return MiniSweAgentHarness(
        model=ModelConfig(
            name="openai/my-model", base_url="http://vllm:8000/v1", api_key_env="K"
        ),
        params=params,
        timeout=2400,
    )


def _launch(instance: FakeSandboxInstance) -> dict:
    """The `mini` invocation itself, not the PATH probe or the mkdir."""
    return [c for c in instance.commands if "--agent-class" in str(c["command"])][0]


def _launch_command(instance: FakeSandboxInstance) -> str:
    return str(_launch(instance)["command"])


def _found(command):
    return FakeJobResult(stdout="/usr/local/bin/mini")


class TestCommand:
    def test_the_agent_is_the_non_interactive_one(self):
        # `mini` defaults to its interactive agent, which blocks on a
        # confirmation prompt nothing in a pod can answer.
        command = MiniSweAgentHarness.spec.render_command(
            {
                "config": "benchmarks/swebench.yaml",
                "workdir": "/testbed",
                "step_timeout": "60",
                "timeout": "2400",
                "model_config": "",
                "model": "m",
                "trajectory": MINI_TRAJECTORY_PATH,
            }
        )
        assert "--agent-class default" in command

    @pytest.mark.asyncio
    async def test_the_environment_is_local_not_docker(self):
        # swebench.yaml targets DockerEnvironment, which would try to launch a
        # container from inside the container.
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert "environment.environment_class=local" in _launch_command(instance)

    @pytest.mark.asyncio
    async def test_the_sandbox_workdir_wins_over_the_config(self):
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert "environment.cwd=/testbed" in _launch_command(instance)

    @pytest.mark.asyncio
    async def test_a_command_gets_120s_by_default(self):
        # Long enough for a compiled language's test suite, and the same as
        # the Harbor path, so the two runs measure the same agent.
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert "environment.timeout=120" in _launch_command(instance)

    @pytest.mark.asyncio
    async def test_the_per_command_timeout_can_be_set(self):
        ctx, instance = _context(_found)
        await _harness(step_timeout=600).rollout(ctx)
        assert "environment.timeout=600" in _launch_command(instance)

    @pytest.mark.asyncio
    async def test_no_temperature_is_sent_unless_asked(self):
        # Unset, the server's default applies — the checkpoint's own.
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert "temperature" not in _launch_command(instance)

    @pytest.mark.asyncio
    async def test_a_temperature_reaches_every_request(self):
        ctx, instance = _context(_found)
        await _harness(temperature=0.9).rollout(ctx)
        assert "-c model.model_kwargs.temperature=0.9" in _launch_command(instance)

    @pytest.mark.asyncio
    async def test_a_temperature_in_the_overrides_still_wins(self):
        # mini merges left to right, so the user's override has to come last.
        ctx, instance = _context(_found)
        await _harness(
            temperature=0.9, config_overrides=["model.model_kwargs.temperature=0.7"]
        ).rollout(ctx)
        command = _launch_command(instance)
        assert command.index("temperature=0.9") < command.index("temperature=0.7")

    @pytest.mark.asyncio
    async def test_the_agent_gets_its_own_wall_clock_guard(self):
        # So a stuck rollout ends inside the agent, with a saved trajectory,
        # instead of being killed from outside.
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert "agent.wall_time_limit_seconds=2400" in _launch_command(instance)

    @pytest.mark.asyncio
    async def test_config_overrides_are_appended_after_the_spec(self):
        ctx, instance = _context(_found)
        await _harness(config_overrides=["agent.step_limit=7"]).rollout(ctx)
        command = _launch_command(instance)
        assert "agent.step_limit=7" in command
        assert command.index("environment.cwd") < command.index("agent.step_limit=7")

    @pytest.mark.asyncio
    async def test_the_task_reaches_mini_unwrapped(self):
        # swebench.yaml already wraps the issue in mini's instance template; a
        # second set of instructions would conflict with it.
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert instance.files[PROMPT_PATH].decode() == "Fix the thing"

    @pytest.mark.asyncio
    async def test_a_non_testbed_checkout_corrects_minis_hardcoded_path(self):
        # swebench.yaml names /testbed twice. `environment.cwd` is overridden;
        # the instance template's prose is not, so on SWE-bench Pro — which
        # checks out at /app — the agent was told to edit a directory that does
        # not exist.
        ctx, instance = _context(_found, workdir="/app")
        await _harness().rollout(ctx)

        prompt = instance.files[PROMPT_PATH].decode()
        assert prompt.endswith("Fix the thing")
        assert "/app" in prompt
        assert "there is no /testbed directory" in prompt

    @pytest.mark.asyncio
    async def test_a_caller_supplied_config_is_left_alone(self):
        # Someone who brought their own config has already said where the
        # repository is; correcting it would be us guessing over them.
        ctx, instance = _context(_found, workdir="/app")
        await _harness(config="my/own.yaml").rollout(ctx)
        assert instance.files[PROMPT_PATH].decode() == "Fix the thing"

    @pytest.mark.asyncio
    async def test_it_runs_from_a_login_shell(self):
        # mini shells out through `sh`, which never reads ~/.bashrc, so the
        # SWE-bench `testbed` conda env has to be in the inherited environment.
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert _launch(instance)["login_shell"] is True


class TestEnvironment:
    @pytest.mark.asyncio
    async def test_the_first_run_wizard_is_disabled(self):
        # Without this mini prompts for a model name and an API key, and the
        # rollout hangs until the harness timeout.
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        env = _launch(instance)["env"]
        assert env["MSWEA_CONFIGURED"] == "true"
        assert env["MSWEA_GLOBAL_CONFIG_DIR"] == MINI_HOME

    @pytest.mark.asyncio
    async def test_cost_lookup_failures_do_not_kill_the_rollout(self):
        # A self-hosted model is never in litellm's price table, and mini turns
        # that lookup miss into a RuntimeError.
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert _launch(instance)["env"]["MSWEA_COST_TRACKING"] == "ignore_errors"

    def test_the_endpoint_is_staged_as_a_config_layer(self):
        # litellm has changed which endpoint env var it honours; api_base is
        # the argument all of them resolve to.
        assert "api_base: " in _harness()._render_config()
        assert '"http://vllm:8000/v1"' in _harness()._render_config()

    def test_no_config_layer_without_an_endpoint(self):
        harness = MiniSweAgentHarness(
            model=ModelConfig(name="anthropic/claude"), params={}, timeout=60
        )
        assert harness._render_config() is None
        # ...and the `-c` group referencing it must disappear with it, or mini
        # dies on a config file that was never staged.
        ctx, _ = _context()
        assert harness._substitutions(ctx)["model_config"] == ""


class TestTrajectory:
    @pytest.mark.asyncio
    async def test_the_trajectory_comes_from_the_file_not_stdout(self):
        # mini's stdout is a human-readable log; the structured record is the
        # JSON it writes to `-o`.
        def responder(command):
            if "command -v" in command:
                return FakeJobResult(stdout="/usr/local/bin/mini")
            return FakeJobResult(stdout="This is mini-swe-agent version 2.4.6")

        ctx, _ = _context(responder)
        result = await _harness().rollout(ctx)
        assert [m["role"] for m in result.messages] == [
            "system",
            "user",
            "assistant",
            "tool",
            "exit",
        ]

    @pytest.mark.asyncio
    async def test_the_agents_own_submission_is_graded(self):
        # mini's SWE-bench prompt makes the model produce and hand in the diff;
        # that submission is the fairest thing to grade.
        ctx, _ = _context(_found)
        result = await _harness().rollout(ctx)
        assert result.patch == SUBMITTED_PATCH
        assert result.submission == SUBMITTED_PATCH

    @pytest.mark.asyncio
    async def test_no_submission_falls_back_to_the_working_tree(self):
        # An agent that hits its step limit never submits but may still have
        # made real edits; discarding them would understate the harness.
        def responder(command):
            if "command -v" in command:
                return FakeJobResult(stdout="/usr/local/bin/mini")
            if "git diff" in command:
                return FakeJobResult(stdout="diff --git a/g b/g\n")
            return FakeJobResult()

        ctx, instance = _context(responder)
        instance.files[MINI_TRAJECTORY_PATH] = json.dumps(
            {"info": {"exit_status": "LimitsExceeded", "submission": ""}, "messages": []}
        ).encode()

        result = await _harness().rollout(ctx)
        assert result.patch == "diff --git a/g b/g\n"

    @pytest.mark.asyncio
    async def test_a_missing_trajectory_file_does_not_fail_the_rollout(self):
        ctx, instance = _context(_found)
        del instance.files[MINI_TRAJECTORY_PATH]
        result = await _harness().rollout(ctx)
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_cost_reaches_the_run_summary(self):
        ctx, _ = _context(_found)
        result = await _harness().rollout(ctx)
        assert result.metrics["cost"] == 0.42


class TestInstallFallback:
    """For pods whose sandbox-tools image predates the bundled `mini`."""

    @staticmethod
    def _absent_then_installed():
        present = {"yes": False}

        def responder(command):
            if command.startswith("command -v mini"):
                if not present["yes"]:
                    return FakeJobResult(exit_code=1)
                return FakeJobResult(stdout="/usr/local/bin/mini")
            if MINI_VERSION_SPEC in command:
                present["yes"] = True
            return FakeJobResult()

        return responder

    @pytest.mark.asyncio
    async def test_a_missing_mini_is_installed_and_then_used(self):
        ctx, instance = _context(self._absent_then_installed())
        result = await _harness().rollout(ctx)
        assert result.exit_status == EXIT_COMPLETED
        assert any("--agent-class" in str(c["command"]) for c in instance.commands)

    @pytest.mark.asyncio
    async def test_nothing_is_installed_when_mini_is_already_there(self):
        ctx, instance = _context(_found)
        await _harness().rollout(ctx)
        assert not any(
            MINI_VERSION_SPEC in str(c["command"]) for c in instance.commands
        )

    @pytest.mark.asyncio
    async def test_the_fallback_can_be_turned_off(self):
        ctx, instance = _context(lambda command: FakeJobResult(exit_code=1))
        result = await _harness(auto_install=False).rollout(ctx)
        assert result.exit_status == EXIT_AGENT_ERROR
        assert not any(
            MINI_VERSION_SPEC in str(c["command"]) for c in instance.commands
        )

    @pytest.mark.asyncio
    async def test_a_failed_install_points_at_its_log(self):
        ctx, _ = _context(lambda command: FakeJobResult(exit_code=1))
        result = await _harness().rollout(ctx)
        assert result.exit_status == EXIT_AGENT_ERROR
        assert "install.log" in result.error

    def test_every_install_lands_in_the_isolated_venv(self):
        # pip-installing into a SWE-bench image's own interpreter would resolve
        # the package under test out from under the agent.
        installs = [
            line
            for line in MINI_INSTALL_COMMAND.splitlines()
            if "pip install" in line and "--upgrade pip" not in line
        ]
        assert installs
        assert all("$VENV" in line for line in installs)
        assert MINI_HOME in MINI_INSTALL_COMMAND

    def test_the_interpreter_search_prefers_named_versions_over_python3(self):
        # Bare `python3` is the testbed conda env on a SWE-bench image, which is
        # routinely older than the 3.10 mini requires.
        search = MINI_INSTALL_COMMAND[MINI_INSTALL_COMMAND.index("for candidate") :]
        assert search.index("python3.10") < search.index("python3;")


class TestParser:
    def test_messages_pass_through_and_info_is_lifted(self):
        messages, info = parse_mini_trajectory(TRAJECTORY)
        assert len(messages) == 5
        assert info["exit_status"] == "Submitted"
        assert info["submission"] == SUBMITTED_PATCH
        assert info["cost"] == 0.42
        assert info["api_calls"] == 7

    def test_an_empty_file_is_reported_not_raised(self):
        messages, info = parse_mini_trajectory("")
        assert messages == []
        assert "empty" in info["error"]
