"""The harness registry and the declarative CLI harnesses."""

import json
import time

import pytest

from orchard_evalkit.config import HarnessConfig, ModelConfig, RunConfig
from orchard_evalkit.harnesses import (
    CodexHarness,
    Harness,
    PiHarness,
    RolloutContext,
    available_harnesses,
    build_harness,
    get_harness_class,
    register_harness,
)
from orchard_evalkit.harnesses import installed_cli
from orchard_evalkit.harnesses.installed_cli import (
    CLAUDE_HOME,
    CLAUDE_MODEL_ALIAS_VARS,
    CODEX_HOME,
    PROMPT_PATH,
    PROMPT_TOKEN,
    STDOUT_MIRROR_PATH,
    ClaudeCodeHarness,
    CliSpec,
    InstalledCliHarness,
    OpencodeHarness,
)
from orchard_evalkit.harnesses.trajectory import PARSERS
from orchard_evalkit.jobs import ExecDispatchError
from orchard_evalkit.models import (
    EXIT_AGENT_ERROR,
    EXIT_COMPLETED,
    EXIT_INFRA_ERROR,
    EXIT_TIMEOUT,
    TaskInstance,
)
from orchard_evalkit.sandbox import EvalSandbox, SandboxGoneError
from tests.fakes import FakeJobResult, FakeResponseError, FakeSandboxInstance


class TestRegistry:
    def test_builtin_harnesses_are_registered(self):
        names = available_harnesses()
        for expected in ("codex", "pi", "mini-swe-agent"):
            assert expected in names

    def test_lookup_by_name(self):
        assert get_harness_class("codex") is CodexHarness

    def test_unknown_name_lists_the_alternatives(self):
        with pytest.raises(KeyError, match="Unknown harness"):
            get_harness_class("not-a-harness")

    def test_build_harness_wires_model_and_params(self):
        harness = build_harness(
            HarnessConfig(name="pi", timeout=120, params={"x": 1}),
            ModelConfig(name="openai/gpt-5"),
        )
        assert isinstance(harness, PiHarness)
        assert harness.timeout == 120
        assert harness.params == {"x": 1}

    def test_duplicate_registration_is_rejected(self):
        # Shadowing a harness would make results impossible to attribute.
        class Duplicate(Harness):
            name = "codex"

            async def rollout(self, ctx):  # pragma: no cover
                raise NotImplementedError

        with pytest.raises(ValueError, match="already registered"):
            register_harness(Duplicate)

    def test_nameless_harness_is_rejected(self):
        class Nameless(Harness):
            async def rollout(self, ctx):  # pragma: no cover
                raise NotImplementedError

        with pytest.raises(ValueError, match="non-empty"):
            register_harness(Nameless)


class TestCliSpecRendering:
    def test_groups_with_empty_placeholders_are_dropped(self):
        # Without this, an unset model would emit a dangling `-m`.
        spec = CliSpec(binary="agent", args=(("exec",), ("-m", "{model}")))
        assert spec.render_command({"model": ""}).endswith("agent exec")

    def test_groups_with_values_are_kept(self):
        spec = CliSpec(binary="agent", args=(("-m", "{model}"),))
        assert "-m gpt-5" in spec.render_command({"model": "gpt-5"})

    def test_argv_prompts_go_through_a_shell_variable(self):
        # The prompt is a whole SWE-bench issue: never interpolate it inline.
        spec = CliSpec(binary="agent", args=((PROMPT_TOKEN,),), prompt_delivery="argv")
        command = spec.render_command({})
        assert f"cat {PROMPT_PATH}" in command
        assert '"$ORCHARD_EVAL_PROMPT"' in command

    def test_stdin_prompts_are_redirected_from_the_staged_file(self):
        spec = CliSpec(binary="agent", args=(("-",),), prompt_delivery="stdin")
        assert spec.render_command({}).endswith(f"< {PROMPT_PATH}")

    def test_values_are_shell_quoted(self):
        spec = CliSpec(binary="agent", args=(("-C", "{workdir}"),))
        assert "'/path with spaces'" in spec.render_command(
            {"workdir": "/path with spaces"}
        )

    def test_extra_args_land_before_a_stdin_redirect(self):
        # Appending after `< file` would put the flag past the redirection,
        # which is legal shell but not what anyone reading it expects.
        spec = CliSpec(binary="agent", args=(("-",),), prompt_delivery="stdin")
        command = spec.render_command({}, extra_args=["--json"])
        assert command.index("--json") < command.index("<")
        assert command.endswith(f"< {PROMPT_PATH}")

    def test_trajectory_flags_follow_the_subcommand(self):
        # `codex --json exec` is rejected outright ("unexpected argument
        # '--json' found"): the flag belongs to the subcommand, not the binary.
        spec = CliSpec(
            binary="agent",
            subcommand=("exec",),
            args=(("-m", "{model}"),),
            trajectory_args=(("--json",),),
        )
        command = spec.render_command({"model": "gpt-5"})
        assert "agent exec --json -m gpt-5" in command

    @pytest.mark.parametrize(
        "harness_cls,expected",
        [
            (CodexHarness, "codex exec --json "),
            (OpencodeHarness, "opencode run --format json "),
        ],
    )
    def test_subcommand_clis_render_verb_before_trajectory_flags(
        self, harness_cls, expected
    ):
        command = harness_cls.spec.render_command(
            {"model": "m", "workdir": "/testbed", "last_message": "/tmp/last.txt"}
        )
        assert expected in command


class TestDroppedEventTypes:
    """Filtering the event stream inside the sandbox.

    pi's ``message_update`` re-sends the whole accumulated message on every
    streamed token, so stdout grows with the square of the message length. Under
    a reasoning model that reached 1.3GB for a single rollout and took the exec
    connection down with it, scoring 116 of 500 SWE-bench Verified instances as
    empty patches. The events never need to leave the pod.
    """

    def test_filter_is_absent_without_the_field(self):
        spec = CliSpec(binary="agent", args=(("run",),), mirror_stdout=True)
        assert "awk" not in spec.render_command({})

    def test_filter_drops_the_named_events(self):
        spec = CliSpec(binary="agent", args=(("run",),), drop_event_types=("noisy",))
        assert '!/^[{]"type":"(noisy)"/' in spec.render_command({})

    def test_several_event_types_become_one_alternation(self):
        spec = CliSpec(binary="agent", args=(("run",),), drop_event_types=("a", "b"))
        assert '"(a|b)"' in spec.render_command({})

    def test_filter_runs_before_the_mirror(self):
        # Behind the tee the pod's own mirror would still take the full volume,
        # which is what fills /tmp on the sandbox.
        spec = CliSpec(
            binary="agent",
            args=(("run",),),
            mirror_stdout=True,
            drop_event_types=("noisy",),
        )
        command = spec.render_command({})
        assert command.index("awk") < command.index("tee")

    def test_filtering_alone_still_sets_pipefail(self):
        # Without it the pipeline reports awk's status, and an agent that exits
        # non-zero on a hit step limit would be recorded as a clean run.
        spec = CliSpec(binary="agent", args=(("run",),), drop_event_types=("noisy",))
        assert "set -o pipefail; agent run |" in spec.render_command({})

    def test_a_deadline_wraps_the_cli_and_not_the_pipeline(self):
        # Killing the pipeline would take `tee` with it mid-write; killing only
        # the CLI closes the pipe and lets the filters drain and flush, which
        # is the entire point of stopping early.
        command = PiHarness.spec.render_command({"model_ref": "m"}, agent_timeout=1740)
        assert "timeout -k 10 1740 pi " in command
        # Nothing after the CLI is under the deadline, so the filters survive
        # it and flush what they have.
        assert "timeout" not in command.split("| awk", 1)[1]
        assert command.index("timeout -k") < command.index("| tee")

    def test_pi_filters_message_update(self):
        command = PiHarness.spec.render_command(
            {"model_ref": "orchard-model", "workdir": "/testbed"}
        )
        assert '!/^[{]"type":"(message_update)"/' in command
        assert command.index("awk") < command.index(STDOUT_MIRROR_PATH)

    @pytest.mark.parametrize(
        "harness_cls", [CodexHarness, ClaudeCodeHarness, OpencodeHarness]
    )
    def test_delta_streaming_clis_are_left_alone(self, harness_cls):
        command = harness_cls.spec.render_command(
            {
                "model": "m",
                "model_ref": "m",
                "workdir": "/testbed",
                "last_message": "/tmp/last.txt",
                "effort": "medium",
            }
        )
        assert "awk" not in command


class _StubHarness(InstalledCliHarness):
    name = "stub-cli"
    description = "test double"
    spec = CliSpec(
        binary="stubagent",
        args=(("run",), ("-m", "{model}"), (PROMPT_TOKEN,)),
        prompt_delivery="argv",
        api_key_env="STUB_API_KEY",
        base_url_env="STUB_BASE_URL",
    )


class _TrajectoryHarness(InstalledCliHarness):
    """Stub that emits codex-shaped events, to exercise the capture path."""

    name = "stub-cli-trajectory"
    description = "test double with trajectory capture"
    spec = CliSpec(
        binary="stubagent",
        args=(("run",), (PROMPT_TOKEN,)),
        prompt_delivery="argv",
        trajectory_args=(("--json",),),
        trajectory_format="codex",
        api_key_env="STUB_API_KEY",
    )


class _PiStreamHarness(InstalledCliHarness):
    """Stub that emits pi-shaped events, to exercise the stop-reason path."""

    name = "stub-cli-pi-stream"
    description = "test double emitting pi events"
    spec = CliSpec(
        binary="stubagent",
        args=(("run",), (PROMPT_TOKEN,)),
        prompt_delivery="argv",
        trajectory_args=(("--mode", "json"),),
        trajectory_format="pi",
        api_key_env="STUB_API_KEY",
    )


class _InstallableHarness(InstalledCliHarness):
    """Stub that knows how to install itself, like mini-swe-agent."""

    name = "stub-cli-installable"
    description = "test double with an install fallback"
    spec = CliSpec(
        binary="stubagent",
        args=(("run",), (PROMPT_TOKEN,)),
        prompt_delivery="argv",
        install_command="install-stubagent",
        api_key_env="STUB_API_KEY",
    )


class _SetupHarness(InstalledCliHarness):
    """Stub with a setup command, like codex's CODEX_HOME creation."""

    name = "stub-cli-setup"
    description = "test double with a setup command"
    spec = CliSpec(
        binary="stubagent",
        args=(("run",), (PROMPT_TOKEN,)),
        prompt_delivery="argv",
        setup_command="mkdir -p /tmp/.stubhome",
        api_key_env="STUB_API_KEY",
    )


CODEX_EVENTS = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"command":"ls","aggregated_output":"a.py","exit_code":0}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"Fixed."}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":9}}',
    ]
)


def _context(responder=None, **params) -> tuple[RolloutContext, FakeSandboxInstance]:
    instance = FakeSandboxInstance(responder=responder)
    sandbox = EvalSandbox(instance, workdir="/testbed")
    task = TaskInstance(
        instance_id="astropy__astropy-12907",
        problem_statement="Fix the thing",
        base_commit="abc123",
        image="img",
    )
    ctx = RolloutContext(sandbox=sandbox, instance=task, config=RunConfig())
    return ctx, instance


class TestInstalledCliHarness:
    @pytest.mark.asyncio
    async def test_happy_path_produces_a_patch(self):
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "git diff" in cmd:
                return FakeJobResult(stdout="diff --git a/f b/f\n")
            return FakeJobResult(stdout="agent ran")

        ctx, _ = _context(responder)
        harness = _StubHarness(model=ModelConfig(name="m1"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_COMPLETED
        assert result.patch == "diff --git a/f b/f\n"
        assert result.metrics["binary"] == "stubagent"

    @pytest.mark.asyncio
    async def test_prompt_is_staged_as_a_file(self):
        ctx, instance = _context(lambda c: FakeJobResult(stdout="/usr/bin/stubagent"))
        harness = _StubHarness(model=ModelConfig(name="m1"), params={}, timeout=60)
        await harness.rollout(ctx)

        prompt = instance.files[PROMPT_PATH].decode()
        assert "Fix the thing" in prompt
        assert "/testbed" in prompt

    @pytest.mark.asyncio
    async def test_credentials_are_passed_per_call(self, monkeypatch):
        monkeypatch.setenv("STUB_API_KEY", "sk-test")
        ctx, instance = _context(lambda c: FakeJobResult(stdout="/usr/bin/stubagent"))
        harness = _StubHarness(
            model=ModelConfig(
                name="m1", api_key_env="STUB_API_KEY", base_url="http://llm:8000/v1"
            ),
            params={},
            timeout=60,
        )
        await harness.rollout(ctx)

        # The first "stubagent" command is the PATH probe; the agent launch is
        # the one that carries credentials.
        agent_call = [
            c for c in instance.commands if "stubagent run" in str(c["command"])
        ][0]
        assert agent_call["env"]["STUB_API_KEY"] == "sk-test"
        assert agent_call["env"]["STUB_BASE_URL"] == "http://llm:8000/v1"

    @pytest.mark.asyncio
    async def test_missing_binary_fails_fast_with_a_useful_message(self):
        ctx, _ = _context(lambda c: FakeJobResult(stdout="", exit_code=1))
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_AGENT_ERROR
        assert "not on PATH" in result.error
        assert "ENABLE_SANDBOX_TOOLS" in result.error

    @pytest.mark.asyncio
    async def test_the_agent_is_launched_by_its_resolved_path(self):
        # The probe runs a plain shell; a harness with login_shell runs the CLI
        # under one, and Debian's /etc/profile assigns PATH outright — dropping
        # the tools directory the probe just found it in. That is exit 127, with
        # an empty patch, before the model is ever called.
        ctx, instance = _context(
            lambda c: FakeJobResult(stdout="/opt/sandbox-tools/bin/stubagent")
        )
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        await harness.rollout(ctx)

        launch = self._launch_command(instance)
        assert "/opt/sandbox-tools/bin/stubagent run" in launch

    @pytest.mark.asyncio
    async def test_a_resolution_that_is_not_a_path_keeps_the_bare_name(self):
        # `command -v` answers with a bare word for a builtin, function or alias.
        ctx, instance = _context(lambda c: FakeJobResult(stdout="stubagent"))
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        await harness.rollout(ctx)

        assert "; stubagent run" in self._launch_command(instance)

    @pytest.mark.asyncio
    async def test_a_binary_that_resolves_but_cannot_run_is_diagnosed(self):
        # The sandbox tools are glibc ELFs mounted into someone else's image. On
        # a musl one `command -v` still finds the wrapper and `execve` still
        # fails, which used to surface only as a bare "exited with code 127"
        # after the pod, the prompt and the config had all been paid for.
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/opt/sandbox-tools/bin/stubagent")
            if "--version" in cmd:
                return FakeJobResult(
                    stderr=(
                        "/opt/sandbox-tools/bin/stubagent: exec: line 18: "
                        "/opt/sandbox-tools/stubagent/stubagent: not found"
                    ),
                    exit_code=127,
                )
            return FakeJobResult()

        ctx, instance = _context(responder)
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_AGENT_ERROR
        assert "cannot execute in this image" in result.error
        # The image is the only thing that identifies the failing population.
        assert "img" in result.error
        assert "musl/Alpine" in result.error
        assert result.metrics["image"] == "img"
        # Nothing was launched, so this is not billed as a model failure.
        assert not any("stubagent run" in str(c["command"]) for c in instance.commands)

    @pytest.mark.asyncio
    async def test_an_unrunnable_binary_still_gets_the_install_fallback(self):
        # An image whose libc cannot load the bundled payload can very often
        # still build a native one, so "present but broken" has to reach the
        # installer that "absent" already reaches.
        calls: list[str] = []

        def responder(cmd):
            calls.append(cmd)
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/local/bin/stubagent")
            if "--version" in cmd:
                # Broken before the install, working after it.
                if any("install-stubagent" in c for c in calls):
                    return FakeJobResult(stdout="stubagent 1.0")
                return FakeJobResult(stderr="Error relocating", exit_code=127)
            return FakeJobResult()

        ctx, instance = _context(responder)
        harness = _InstallableHarness(
            model=ModelConfig(name="m"), params={}, timeout=60
        )
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_COMPLETED
        assert any("install-stubagent" in str(c["command"]) for c in instance.commands)
        assert "/usr/local/bin/stubagent run" in self._launch_command(instance)

    @pytest.mark.asyncio
    async def test_a_failed_install_reports_the_execution_fault_not_the_path(self):
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/local/bin/stubagent")
            if "--version" in cmd:
                return FakeJobResult(stderr="Error relocating", exit_code=127)
            return FakeJobResult()

        ctx, _ = _context(responder)
        harness = _InstallableHarness(
            model=ModelConfig(name="m"), params={}, timeout=60
        )
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_AGENT_ERROR
        assert "cannot execute in this image" in result.error
        assert "not on PATH" not in result.error
        assert "install.log" in result.error

    @pytest.mark.asyncio
    async def test_the_image_is_recorded_on_a_successful_rollout(self):
        # A finished run otherwise has no record of which image ran, and
        # image-specific faults are exactly what needs one.
        ctx, _ = _context(lambda c: FakeJobResult(stdout="/usr/bin/stubagent"))
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.metrics["image"] == "img"

    @staticmethod
    def _launch_command(instance) -> str:
        return next(
            str(c["command"])
            for c in instance.commands
            if "stubagent run" in str(c["command"])
        )

    @pytest.mark.asyncio
    async def test_nonzero_exit_still_keeps_the_patch(self):
        # Agents routinely exit non-zero on a hit step limit while leaving a
        # perfectly gradable diff behind; discarding it would understate them.
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "--version" in cmd:
                # The runnability probe has to pass, or the agent never launches
                # and there is no non-zero exit to test.
                return FakeJobResult(stdout="stubagent 1.0")
            if "git diff" in cmd:
                return FakeJobResult(stdout="diff --git a/f b/f\n")
            return FakeJobResult(exit_code=1, stderr="step limit")

        ctx, _ = _context(responder)
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_AGENT_ERROR
        assert result.patch == "diff --git a/f b/f\n"

    @staticmethod
    def _pi_responder(stream, diff=""):
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "git diff" in cmd:
                return FakeJobResult(stdout=diff)
            return FakeJobResult(stdout=stream)

        return responder

    #: A run the provider cut off at its output-token cap. The CLI still exits
    #: 0 and still reports a complete `agent_end`.
    TRUNCATED_STREAM = (
        '{"type":"agent_end","messages":[{"role":"assistant","content":'
        '[{"type":"thinking","thinking":"wait"}],"stopReason":"length"}]}'
    )
    FINISHED_STREAM = (
        '{"type":"agent_end","messages":[{"role":"assistant","content":'
        '[{"type":"text","text":"Done."}],"stopReason":"stop"}]}'
    )

    @pytest.mark.asyncio
    async def test_a_truncated_completion_is_not_a_completed_rollout(self):
        # The CLI ends its session on the first truncated completion instead
        # of re-prompting, and exits 0 with the tree untouched. Left as
        # `completed`, that is indistinguishable from an agent that looked
        # around and found nothing to fix.
        ctx, _ = _context(self._pi_responder(self.TRUNCATED_STREAM))
        harness = _PiStreamHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_AGENT_ERROR
        assert "output-token limit" in (result.error or "")
        assert result.metrics["length_stops"] == 1
        assert result.metrics["stop_reason"] == "length"

    @pytest.mark.asyncio
    async def test_a_truncated_rollout_keeps_the_diff_it_already_made(self):
        # Truncation says the run stopped early, not that its work is void —
        # and a diff already on disk still grades.
        ctx, _ = _context(
            self._pi_responder(self.TRUNCATED_STREAM, diff="diff --git a/f b/f\n")
        )
        harness = _PiStreamHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.patch == "diff --git a/f b/f\n"
        assert result.exit_status == EXIT_AGENT_ERROR

    @pytest.mark.asyncio
    async def test_a_run_that_stopped_on_its_own_stays_completed(self):
        ctx, _ = _context(self._pi_responder(self.FINISHED_STREAM))
        harness = _PiStreamHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_COMPLETED
        assert result.metrics["length_stops"] == 0

    def test_a_spent_budget_is_a_timeout_not_a_transport_fault(self):
        # The orchestrator abandons the exec stream when a killed pipeline
        # outruns its teardown window, so a rollout that used its whole budget
        # is indistinguishable from a dropped connection by exit code alone.
        # Only the elapsed time separates them, and the answer decides whether
        # the runner spends two more pods on it.
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=1800)
        status, error = harness._classify_nonzero_exit(-1, 1820.0, 0)
        assert status == EXIT_TIMEOUT
        assert "1800s deadline" in error

    def test_a_stream_that_died_early_is_still_a_transport_fault(self):
        # Retrying this one on a fresh pod is the right move, so it must not be
        # swept into the timeout bucket.
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=1800)
        status, error = harness._classify_nonzero_exit(-1, 611.0, 0)
        assert status == EXIT_AGENT_ERROR
        assert "without an exit frame" in error

    def test_an_exit_code_the_agent_chose_is_reported_as_its_own(self):
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=1800)
        status, error = harness._classify_nonzero_exit(1, 1900.0, 42)
        assert status == EXIT_AGENT_ERROR
        assert "exited with code 1" in error

    @pytest.mark.asyncio
    async def test_a_dropped_agent_launch_is_infra_and_the_pod_is_left_alone(self):
        # The SDK used to answer this by launching the agent a second time in
        # the same pod. Now it is an infra error, which the runner retries on a
        # fresh pod — and the diff is not read from this one.
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "--version" in cmd:
                return FakeJobResult(stdout="stub 1.0")
            if "stubagent run" in cmd:
                raise ExecDispatchError("POST failed after it was sent")
            return FakeJobResult()

        ctx, instance = _context(responder)
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_INFRA_ERROR
        assert "ExecDispatchError" in (result.error or "")
        sent = [str(c["command"]) for c in instance.commands]
        assert sum("stubagent run" in c for c in sent) == 1
        assert not any("git diff" in c for c in sent)

    @pytest.mark.asyncio
    async def test_a_timeout_is_not_downgraded_to_infra_by_a_lost_diff(self):
        # The sandbox stops answering at the moment the deadline is enforced,
        # so patch extraction fails too. Calling that infra would send the
        # instance back for two more full-length attempts that end the same way.
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "--version" in cmd:
                return FakeJobResult(stdout="stub 1.0")
            if "git diff" in cmd:
                raise SandboxGoneError("sbx-test", RuntimeError("404"))
            time.sleep(1.05)
            return FakeJobResult(exit_code=-1)

        ctx, _ = _context(responder)
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=1)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_TIMEOUT
        assert result.exit_status != EXIT_INFRA_ERROR

    @pytest.mark.asyncio
    async def test_timeout_margin_bounds_the_agent_inside_the_sandbox(self):
        ctx, instance = _context(lambda c: FakeJobResult(stdout="/usr/bin/stubagent"))
        harness = _StubHarness(
            model=ModelConfig(name="m"), params={"timeout_margin": 60}, timeout=1800
        )
        await harness.rollout(ctx)

        agent = next(
            str(c["command"])
            for c in instance.commands
            if "stubagent run" in str(c["command"])
        )
        # 1800 - 60, wrapping the CLI itself.
        assert "timeout -k 10 1740 /usr/bin/stubagent run" in agent
        assert harness._agent_deadline() == 1740

    @pytest.mark.asyncio
    async def test_a_sandbox_without_timeout_falls_back_instead_of_failing(self):
        # A missing `timeout` would otherwise exit 127 on every rollout of the
        # run — much worse than the loss the margin exists to prevent.
        def responder(cmd):
            if "command -v timeout" in cmd:
                return FakeJobResult(stdout="", exit_code=1)
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            return FakeJobResult(stdout="ran")

        ctx, instance = _context(responder)
        harness = _StubHarness(
            model=ModelConfig(name="m"), params={"timeout_margin": 60}, timeout=1800
        )
        result = await harness.rollout(ctx)

        agent = next(
            str(c["command"])
            for c in instance.commands
            if "stubagent run" in str(c["command"])
        )
        assert "timeout -k" not in agent
        assert result.exit_status == EXIT_COMPLETED

    @pytest.mark.parametrize("margin", ["soon", 0, -5, 1800, 4000])
    def test_a_margin_that_leaves_no_budget_is_rejected(self, margin):
        harness = _StubHarness(
            model=ModelConfig(name="m"), params={"timeout_margin": margin}, timeout=1800
        )
        with pytest.raises(ValueError, match="timeout_margin"):
            harness._agent_deadline()

    def test_no_margin_leaves_the_command_alone(self):
        harness = _StubHarness(model=ModelConfig(name="m"), params={}, timeout=1800)
        assert harness._agent_deadline() is None

    @pytest.mark.asyncio
    async def test_extra_args_are_appended(self):
        ctx, instance = _context(lambda c: FakeJobResult(stdout="/usr/bin/stubagent"))
        harness = _StubHarness(
            model=ModelConfig(name="m"), params={"extra_args": ["--json"]}, timeout=60
        )
        await harness.rollout(ctx)
        assert any("--json" in str(c["command"]) for c in instance.commands)

    @pytest.mark.asyncio
    async def test_custom_prompt_template(self):
        ctx, instance = _context(lambda c: FakeJobResult(stdout="/usr/bin/stubagent"))
        harness = _StubHarness(
            model=ModelConfig(name="m"),
            params={"prompt_template": "ONLY: {problem_statement}"},
            timeout=60,
        )
        await harness.rollout(ctx)
        assert instance.files[PROMPT_PATH].decode() == "ONLY: Fix the thing"


class TestBuiltinSpecs:
    def test_codex_bypasses_its_own_sandbox(self):
        # The pod already is the sandbox; a second jail buys nothing.
        flat = [tok for group in CodexHarness.spec.args for tok in group]
        assert "--dangerously-bypass-approvals-and-sandbox" in flat
        assert CodexHarness.spec.prompt_delivery == "stdin"

    def test_pi_runs_without_persisting_a_session(self):
        flat = [tok for group in PiHarness.spec.args for tok in group]
        assert "--print" in flat
        assert "--no-session" in flat

    def test_pi_declares_the_endpoint_as_a_custom_provider(self):
        # pi validates --model against its own catalog and exits with
        # "Model ... not found" for a served id it has never heard of.
        harness = PiHarness(
            model=ModelConfig(name="/models/Qwen3.5-35B-A3B", base_url="http://s/v1"),
            params={},
            timeout=60,
        )
        config = json.loads(harness._render_config())
        provider = config["providers"]["orchard"]
        assert provider["baseUrl"] == "http://s/v1"
        assert provider["api"] == "openai-completions"
        assert provider["apiKey"] == f"${PiHarness.spec.api_key_env}"
        assert provider["models"][0]["id"] == "/models/Qwen3.5-35B-A3B"

    def test_pi_omits_token_limits_unless_they_are_configured(self):
        # The right context window is the serving engine's; a default invented
        # here would either hold a context the server refuses or waste one.
        harness = PiHarness(
            model=ModelConfig(name="m", base_url="http://s/v1"), params={}, timeout=60
        )
        config = json.loads(harness._render_config())
        model = config["providers"]["orchard"]["models"][0]
        assert "maxTokens" not in model
        assert "contextWindow" not in model

    def test_pi_writes_configured_token_limits_into_models_json(self):
        # pi's own default caps output at 16384, and it ends the whole session
        # on the first completion that hits the cap.
        harness = PiHarness(
            model=ModelConfig(name="m", base_url="http://s/v1"),
            params={"max_tokens": 32768, "context_window": 262144},
            timeout=60,
        )
        config = json.loads(harness._render_config())
        model = config["providers"]["orchard"]["models"][0]
        assert model["maxTokens"] == 32768
        assert model["contextWindow"] == 262144

    @pytest.mark.parametrize("value", ["lots", 0, -1])
    def test_pi_rejects_a_nonsense_token_limit(self, value):
        # Rendering it anyway would produce a models.json pi fails to load on
        # every instance of the run.
        harness = PiHarness(
            model=ModelConfig(name="m", base_url="http://s/v1"),
            params={"max_tokens": value},
            timeout=60,
        )
        with pytest.raises(ValueError, match="max_tokens"):
            harness._render_config()

    def test_pi_selects_the_model_by_alias_not_by_path(self):
        # pi reads --model as an optional `provider/id`, so a served id that is
        # a filesystem path cannot be passed through verbatim.
        harness = PiHarness(
            model=ModelConfig(name="/models/Qwen3.5-35B-A3B", base_url="http://s/v1"),
            params={},
            timeout=60,
        )
        assert harness._model_ref() == PiHarness.spec.model_alias
        assert "/" not in PiHarness.spec.model_alias

    def test_pi_falls_back_to_the_raw_model_without_an_endpoint(self):
        harness = PiHarness(model=ModelConfig(name="sonnet"), params={}, timeout=60)
        assert harness._model_ref() == "sonnet"

    def test_codex_creates_its_home_before_running(self):
        # Verified against codex-cli 0.149.1: with CODEX_HOME set to a path
        # that does not exist, codex aborts with "Error finding codex home"
        # and never contacts the model. It will not create the dir itself.
        spec = CodexHarness.spec
        assert spec.env["CODEX_HOME"] == CODEX_HOME
        assert f"mkdir -p {CODEX_HOME}" in spec.setup_command

    def test_codex_home_is_not_under_the_temp_dir(self):
        # codex refuses to create its PATH helper binaries when CODEX_HOME is
        # under the system temp dir, and warns instead of installing them.
        assert not CODEX_HOME.startswith("/tmp")

    def test_codex_routes_to_the_configured_endpoint(self):
        # codex reads OPENAI_BASE_URL only for its built-in provider, which
        # talks to api.openai.com over the Responses API regardless — every
        # rollout 401s there unless a provider is declared in config.toml.
        harness = CodexHarness(
            model=ModelConfig(name="m", base_url="http://vllm.local:8000/v1"),
            params={},
            timeout=60,
        )
        config = harness._render_config()
        assert 'base_url = "http://vllm.local:8000/v1"' in config
        assert 'model_provider = "orchard"' in config
        # codex#10157 made "chat" a startup error, so the default has to be the
        # protocol current codex actually speaks.
        assert 'wire_api = "responses"' in config
        assert f'env_key = "{CodexHarness.spec.api_key_env}"' in config

    def test_codex_config_disables_namespace_tools(self):
        # codex groups these tools into Responses API `{"type": "namespace"}`
        # entries that only OpenAI's endpoint accepts; vLLM/SGLang reject the
        # request body and every rollout dies on its first turn.
        harness = CodexHarness(
            model=ModelConfig(name="m", base_url="http://vllm.local:8000/v1"),
            params={},
            timeout=60,
        )
        config = harness._render_config()
        assert 'web_search = "disabled"' in config
        assert "multi_agent = false" in config
        assert "apps = false" in config

    def test_codex_config_disables_reasoning_summaries(self):
        # Codex replays reasoning as `summary: [{"type": "summary_text"}]`,
        # which a server modelling reasoning as `reasoning_text` rejects — and
        # it only surfaces on turn 2, after a turn's work is already spent.
        harness = CodexHarness(
            model=ModelConfig(name="m", base_url="http://vllm.local:8000/v1"),
            params={},
            timeout=60,
        )
        config = harness._render_config()
        assert "model_supports_reasoning_summaries = false" in config
        assert 'model_reasoning_summary = "none"' in config
        # Not a silent default: a run that never asked for an effort has to
        # stay byte-identical to the ones already recorded.
        assert "model_reasoning_effort" not in config

    def _codex_config(self, **params):
        harness = CodexHarness(
            model=ModelConfig(name="m", base_url="http://vllm.local:8000/v1"),
            params=params,
            timeout=60,
        )
        return harness._render_config()

    def test_codex_reasoning_effort_reaches_the_staged_config(self):
        # Harbor's codex agent passes `-c model_reasoning_effort=high` by
        # default; this path passed nothing, which is the largest measured
        # difference between the two on SWE-bench Pro.
        config = self._codex_config(reasoning_effort="high")
        assert 'model_reasoning_effort = "high"' in config

    def test_codex_reasoning_effort_is_rejected_up_front(self):
        # Every instance of the run would otherwise die at codex startup with
        # an error naming config.toml rather than the setting that was typed.
        with pytest.raises(ValueError, match="reasoning_effort"):
            self._codex_config(reasoning_effort="maximum")

    def test_codex_reasoning_summaries_can_be_turned_back_on(self):
        # Codex gates the whole `reasoning` object on this key, so an effort
        # set while it is off may never reach the wire.
        config = self._codex_config(reasoning_effort="high", reasoning_summaries=True)
        assert "model_supports_reasoning_summaries = true" in config
        assert 'model_reasoning_effort = "high"' in config

    def test_codex_wire_api_is_overridable(self):
        # The escape hatch for a server whose /v1/responses route is missing.
        harness = CodexHarness(
            model=ModelConfig(name="m", base_url="http://vllm.local:8000/v1"),
            params={"wire_api": "chat"},
            timeout=60,
        )
        assert 'wire_api = "chat"' in harness._render_config()

    def test_unknown_wire_api_is_rejected(self):
        # Passed through, this fails inside codex on every instance with an
        # error naming config.toml rather than the setting the user typed.
        harness = CodexHarness(
            model=ModelConfig(name="m", base_url="http://vllm.local:8000/v1"),
            params={"wire_api": "completions"},
            timeout=60,
        )
        with pytest.raises(ValueError, match="wire_api"):
            harness._render_config()

    def test_codex_needs_no_config_without_a_base_url(self):
        harness = CodexHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        assert harness._render_config() is None

    def test_codex_always_has_a_credential_to_read(self):
        # codex aborts when the provider's env_key resolves to nothing, and a
        # self-hosted server started without --api-key accepts any value.
        spec = CodexHarness.spec
        assert spec.env[spec.api_key_env]

    @pytest.mark.parametrize(
        "harness_cls,expected_format",
        [
            (CodexHarness, "codex"),
            (PiHarness, "pi"),
            (ClaudeCodeHarness, "claude"),
            (OpencodeHarness, "opencode"),
        ],
    )
    def test_every_cli_harness_captures_a_trajectory(
        self, harness_cls, expected_format
    ):
        # Without an event stream these CLIs print only prose (or only the
        # final message), and there is no trajectory to recover at all.
        assert harness_cls.spec.trajectory_args, (
            f"{harness_cls.name} has no trajectory_args — it would produce no "
            "usable rollout data"
        )
        assert harness_cls.spec.trajectory_format == expected_format
        assert expected_format in PARSERS

    def test_claude_passes_verbose_with_stream_json(self):
        # Claude Code refuses --output-format=stream-json under -p without it.
        flat = [
            tok for group in ClaudeCodeHarness.spec.trajectory_args for tok in group
        ]
        assert "stream-json" in flat
        assert "--verbose" in flat


class TestClaudeCodeSpeaksTheMessagesApi:
    """Claude Code is the one CLI here that is not an OpenAI client.

    It posts to ``{ANTHROPIC_BASE_URL}/v1/messages``, which sglang v0.5.18
    serves natively — so nothing has to translate, but the endpoint and the
    credential are both spelled differently from every sibling harness. Each
    assertion below stands for a failure that produces an empty patch and no
    diagnosable error: a 401 on every request, or a 404 on every request.
    """

    SESSION_URL = "http://h:30021/session/astropy__astropy-12907/v1"

    def _harness(self, **params):
        return ClaudeCodeHarness(
            model=ModelConfig(name="/models/iter_0000042_hf", base_url=self.SESSION_URL),
            params=params,
            timeout=3600,
        )

    def test_the_credential_goes_in_the_bearer_variable(self):
        # ANTHROPIC_API_KEY is sent as X-Api-Key, and sglang's auth middleware
        # reads Authorization only — so the obvious variable 401s every request.
        assert ClaudeCodeHarness.spec.api_key_env == "ANTHROPIC_AUTH_TOKEN"

    def test_there_is_always_a_credential_to_read(self):
        # With none, Claude Code looks for stored OAuth credentials, finds none,
        # and exits asking to be logged in — under -p there is nobody to ask.
        spec = ClaudeCodeHarness.spec
        assert spec.env[spec.api_key_env]

    def test_a_real_credential_replaces_the_placeholder(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_TEST_KEY", "sk-local")
        harness = ClaudeCodeHarness(
            model=ModelConfig(
                name="m", base_url=self.SESSION_URL, api_key_env="CLAUDE_TEST_KEY"
            ),
            params={},
            timeout=3600,
        )
        assert harness._build_env()["ANTHROPIC_AUTH_TOKEN"] == "sk-local"

    def test_the_endpoint_loses_the_v1_the_cli_re_adds(self):
        env = self._harness()._build_env()
        assert env["ANTHROPIC_BASE_URL"] == (
            "http://h:30021/session/astropy__astropy-12907"
        )

    def test_the_model_base_url_keeps_its_v1(self):
        # router.close_url maps `.../session/<sid>/v1` back to
        # `/router/session/<sid>`; strip it at the source and teardown silently
        # stops releasing sessions.
        harness = self._harness()
        harness._build_env()
        assert harness.model.base_url == self.SESSION_URL

    @pytest.mark.parametrize("var", CLAUDE_MODEL_ALIAS_VARS)
    def test_every_model_alias_points_at_the_served_model(self, var):
        # Compaction, titling and subagents resolve through these independently
        # of --model. Left unset, part of every rollout asks this fleet for a
        # `claude-3-5-haiku-*` it does not serve, and dies partway through for a
        # reason the trajectory does not record.
        assert self._harness()._build_env()[var] == "/models/iter_0000042_hf"

    def test_extended_thinking_is_off_by_default(self):
        # sglang rejects `redacted_thinking` in history outright, and under
        # --reasoning-parser qwen3 emits thinking blocks with no signature,
        # which Claude Code replays verbatim on the next turn.
        assert self._harness()._build_env()["MAX_THINKING_TOKENS"] == "0"

    def test_the_output_cap_matches_pi(self):
        # A cap that differs between harnesses is the difference a claude-code
        # vs pi comparison would actually be measuring.
        assert self._harness()._build_env()["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"

    def test_the_caps_are_overridable(self):
        env = self._harness(max_tokens=32768, max_thinking_tokens=4096)._build_env()
        assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32768"
        assert env["MAX_THINKING_TOKENS"] == "4096"

    def test_effort_is_pinned_to_a_level_the_server_accepts(self):
        # Claude Code defaults output_config.effort to "high"; sglang forwards
        # it as `reasoning_effort` and Qwen3's chat template raises on anything
        # but xhigh/medium/low, which reaches the CLI as a 500. Unpinned, the
        # first request of every rollout fails and the run ends on agent_error
        # with an empty patch — the failure this flag exists to prevent. It is
        # the fallback level, not the effective one: the session router deletes
        # the field, and those rollouts run at the template's own xhigh.
        ctx, _ = _context()
        assert "--effort medium" in self._harness()._build_command(ctx)

    def test_the_effort_level_is_overridable(self):
        ctx, _ = _context()
        command = self._harness(effort_fallback="low")._build_command(ctx)
        assert "--effort low" in command

    def test_an_empty_effort_drops_the_flag(self):
        # For an endpoint whose template takes the CLI's own default.
        ctx, _ = _context()
        command = self._harness(effort_fallback="")._build_command(ctx)
        assert "--effort" not in command

    def test_an_unknown_effort_level_is_rejected(self):
        # Passed through, the CLI rejects it at startup on every instance of
        # the run, with an error that names the flag and not the config key.
        ctx, _ = _context()
        with pytest.raises(ValueError, match="effort_fallback"):
            self._harness(effort_fallback="maximum")._build_command(ctx)

    def test_the_old_effort_key_is_rejected_rather_than_ignored(self):
        # It was renamed because it read as the level the rollout runs at, and
        # under the session router it is not: the router strips the field and
        # the model runs at its template default. A config still spelling it
        # the old way would otherwise silently get this one's default, so say
        # so instead.
        ctx, _ = _context()
        with pytest.raises(ValueError, match="effort_fallback"):
            self._harness(effort="low")._build_command(ctx)

    def test_bypass_permissions_is_allowed_to_take_effect(self):
        # Claude Code refuses --permission-mode bypassPermissions when running
        # as root unless IS_SANDBOX is set, and every task image runs as root.
        assert ClaudeCodeHarness.spec.env["IS_SANDBOX"] == "1"

    def test_its_state_lives_somewhere_writable(self):
        # A task image's HOME is not reliably writable; CODEX_HOME made the
        # same move for the same reason.
        assert ClaudeCodeHarness.spec.env["CLAUDE_CONFIG_DIR"] == CLAUDE_HOME
        assert CLAUDE_HOME in (ClaudeCodeHarness.spec.setup_command or "")

    def test_stdout_is_mirrored(self):
        # stream-json goes to stdout only, and a timed-out exec returns none of
        # it — without the mirror every timeout scores as an empty patch.
        assert ClaudeCodeHarness.spec.mirror_stdout is True


class TestTrajectoryCapture:
    #: The launch command is `stubagent [--json] run "$PROMPT"`, so match on the
    #: binary while excluding the two probes that precede it — the PATH lookup
    #: and the `stubagent --version` run that proves the CLI can execute —
    #: rather than on an exact arg order.
    @staticmethod
    def _is_agent_call(command: str) -> bool:
        return (
            "stubagent" in command
            and "command -v" not in command
            and "--version" not in command
        )

    def _ctx_and_instance(self, stdout: str):
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "git diff" in cmd:
                return FakeJobResult(stdout="diff --git a/f b/f\n")
            if TestTrajectoryCapture._is_agent_call(cmd):
                return FakeJobResult(stdout=stdout)
            return FakeJobResult()

        return _context(responder)

    def _agent_command(self, instance) -> str:
        return next(
            str(c["command"])
            for c in instance.commands
            if self._is_agent_call(str(c["command"]))
        )

    @pytest.mark.asyncio
    async def test_events_are_parsed_into_messages(self):
        ctx, _ = self._ctx_and_instance(CODEX_EVENTS)
        harness = _TrajectoryHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert [m["extra"]["kind"] for m in result.messages] == [
            "command_execution",
            "agent_message",
        ]
        assert result.trajectory_extra["format"] == "codex"

    @pytest.mark.asyncio
    async def test_a_pod_reaped_before_extraction_keeps_the_trajectory(self):
        # The rollout is already paid for by the time the diff is read, so a
        # vanished pod must cost the patch and nothing else — and it is an
        # infrastructure failure, not an agent one.
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "git diff" in cmd:
                raise FakeResponseError(404)
            return FakeJobResult(stdout=CODEX_EVENTS)

        ctx, _ = _context(responder)
        harness = _TrajectoryHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)

        assert result.exit_status == EXIT_INFRA_ERROR
        assert result.patch == ""
        assert "no longer exists" in (result.error or "")
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_turn_and_tool_counts_reach_metrics(self):
        ctx, _ = self._ctx_and_instance(CODEX_EVENTS)
        harness = _TrajectoryHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)
        assert result.metrics["turns"] == 1
        assert result.metrics["tool_calls"] == 1
        assert result.metrics["usage"]["input_tokens"] == 100

    @pytest.mark.asyncio
    async def test_trajectory_flags_are_on_the_command_by_default(self):
        ctx, instance = self._ctx_and_instance("")
        harness = _TrajectoryHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        await harness.rollout(ctx)
        assert "--json" in self._agent_command(instance)

    @pytest.mark.asyncio
    async def test_capture_can_be_disabled(self):
        ctx, instance = self._ctx_and_instance(CODEX_EVENTS)
        harness = _TrajectoryHarness(
            model=ModelConfig(name="m"),
            params={"capture_trajectory": False},
            timeout=60,
        )
        result = await harness.rollout(ctx)
        assert result.messages == []
        assert "--json" not in self._agent_command(instance)

    @pytest.mark.asyncio
    async def test_unparseable_output_keeps_the_raw_stream(self):
        # The run is still valid; only the normalized view is unavailable.
        ctx, _ = self._ctx_and_instance("this is not JSON at all")
        harness = _TrajectoryHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        result = await harness.rollout(ctx)
        assert result.messages == []
        assert result.stdout == "this is not JSON at all"
        # ...and the patch is unaffected.
        assert result.patch == "diff --git a/f b/f\n"

    @pytest.mark.asyncio
    async def test_setup_command_runs_before_the_agent(self):
        ctx, instance = self._ctx_and_instance("")
        harness = _SetupHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        await harness.rollout(ctx)

        commands = [str(c["command"]) for c in instance.commands]
        setup_at = next(i for i, c in enumerate(commands) if "mkdir -p" in c)
        agent_at = next(i for i, c in enumerate(commands) if self._is_agent_call(c))
        assert setup_at < agent_at


class TestRawTrajectoryDump:
    """The untouched streams are what a failed model/sandbox exchange has to be
    diagnosed from: the sandbox is gone by the time anyone looks."""

    def _ctx(self, tmp_path, stdout, stderr=""):
        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "git diff" in cmd:
                return FakeJobResult(stdout="")
            if "stubagent" in cmd:
                return FakeJobResult(stdout=stdout, stderr=stderr)
            return FakeJobResult()

        ctx, _ = _context(responder)
        ctx.artifacts_dir = tmp_path
        return ctx

    @pytest.mark.asyncio
    async def test_raw_stream_is_written_verbatim(self, tmp_path):
        ctx = self._ctx(tmp_path, CODEX_EVENTS, stderr="ERROR 404 /v1/responses")
        harness = _TrajectoryHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        await harness.rollout(ctx)

        assert (tmp_path / "trajectory.raw.jsonl").read_text() == CODEX_EVENTS
        assert (tmp_path / "agent.stderr.log").read_text() == "ERROR 404 /v1/responses"

    @pytest.mark.asyncio
    async def test_unparseable_stream_is_still_dumped(self, tmp_path):
        # This is precisely the case the dump exists for.
        ctx = self._ctx(tmp_path, "not json")
        harness = _TrajectoryHarness(model=ModelConfig(name="m"), params={}, timeout=60)
        await harness.rollout(ctx)
        assert (tmp_path / "trajectory.raw.jsonl").read_text() == "not json"

    @pytest.mark.asyncio
    async def test_dump_can_be_disabled(self, tmp_path):
        ctx = self._ctx(tmp_path, CODEX_EVENTS, stderr="noise")
        harness = _TrajectoryHarness(
            model=ModelConfig(name="m"),
            params={"dump_raw_trajectory": False},
            timeout=60,
        )
        await harness.rollout(ctx)
        assert not (tmp_path / "trajectory.raw.jsonl").exists()
        assert not (tmp_path / "agent.stderr.log").exists()


class TestSessionClose:
    """The rollout tells the session router when it is done with an engine.

    Without it the router has to infer the end of a rollout from silence, and
    that inference is biased: phantom load accumulates in proportion to how
    fast an engine retires rollouts, so the fastest engine in the fleet is the
    one that stops being given work.
    """

    @staticmethod
    def _responder(cmd):
        if "command -v" in cmd:
            return FakeJobResult(stdout="/usr/bin/stubagent")
        if "git diff" in cmd:
            return FakeJobResult(stdout="diff --git a/f b/f\n")
        return FakeJobResult(stdout="agent ran")

    @staticmethod
    def _record(monkeypatch):
        closed = []

        async def fake_close(endpoint, **kwargs):
            closed.append(endpoint)
            return endpoint is not None

        monkeypatch.setattr(installed_cli, "close_session", fake_close)
        return closed

    @pytest.mark.asyncio
    async def test_the_rollouts_session_is_closed(self, monkeypatch):
        closed = self._record(monkeypatch)
        ctx, _ = _context(self._responder)
        harness = _StubHarness(
            model=ModelConfig(
                name="m1", base_url="http://h:30021/v1", routing="session"
            ),
            params={},
            timeout=60,
        )
        await harness.rollout(ctx)
        # The endpoint the agent was actually pointed at, so the id closed is
        # the id handed out rather than one derived a second time.
        assert closed == [f"http://h:30021/session/{ctx.instance.instance_id}/v1"]

    @pytest.mark.asyncio
    async def test_a_run_without_a_router_still_goes_through_one_decision(
        self, monkeypatch
    ):
        # close_session is what decides there is nothing to close, so calling
        # it unconditionally keeps that judgement in one place.
        closed = self._record(monkeypatch)
        ctx, _ = _context(self._responder)
        harness = _StubHarness(model=ModelConfig(name="m1"), params={}, timeout=60)
        await harness.rollout(ctx)
        assert closed == [None]

    @pytest.mark.asyncio
    async def test_a_timed_out_rollout_still_closes(self, monkeypatch):
        # The case that matters most: a trial abandoned at its timeout is
        # exactly the one whose slot would otherwise stay reserved.
        closed = self._record(monkeypatch)

        def responder(cmd):
            if "command -v" in cmd:
                return FakeJobResult(stdout="/usr/bin/stubagent")
            if "--version" in cmd:
                return FakeJobResult(stdout="stub 1.0")
            if "git diff" in cmd:
                return FakeJobResult(stdout="")
            time.sleep(1.05)
            return FakeJobResult(exit_code=-1)

        ctx, _ = _context(responder)
        harness = _StubHarness(
            model=ModelConfig(
                name="m1", base_url="http://h:30021/v1", routing="session"
            ),
            params={},
            timeout=1,
        )
        result = await harness.rollout(ctx)
        assert result.exit_status == EXIT_TIMEOUT
        assert len(closed) == 1
