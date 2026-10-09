"""The install policy for agents whose CLI the pod already carries.

What these pin down is a distinction that cost 176 SWE-bench Pro trials: a
payload CLI being *present* is not the same as it being *usable*. Every sandbox
has ``pi`` and ``mini`` on PATH, but on a musl image the glibc payload resolves
and then fails to execve — so the check has to run the binary, and the fallback
has to know the difference between "no payload here" (defer to Harbor) and
"payload cannot work here" (say so).
"""

from __future__ import annotations

import logging
import stat
import subprocess

import pytest
import yaml

pytest.importorskip("harbor", reason="Harbor is not installed")

from harbor.agents.installed.mini_swe_agent import MiniSweAgent as StockMini  # noqa: E402
from harbor.agents.installed.pi import Pi as StockPi  # noqa: E402

from harbor_orchard.agents import (  # noqa: E402
    STDBUF_SHIM,
    TOOLS_DIR,
    ClaudeCode,
    Codex,
    DirtyWorkingTreeError,
    MiniSweAgent,
    PayloadUnusableError,
    Pi,
    _CleanTreeGate,
    _ModelPatchCapture,
)
from harbor_orchard.settings import (  # noqa: E402
    CLEAN_TREE_TIMEOUT_S,
    MODEL_PATCH_TIMEOUT_S,
    REQUIRE_CLEAN_TREE_ENV_VAR,
)
from harbor_orchard.shell import CLEAN_TREE_PREFIX, MODEL_PATCH_PATH  # noqa: E402


class _Result:
    def __init__(self, return_code: int):
        self.return_code = return_code
        self.stdout = ""
        self.stderr = ""


class _FakeEnvironment:
    """Answers each exec by the first rule whose substring matches the command.

    Rules are ``(substring, return_code)`` in order, defaulting to a non-zero
    exit — an unrecognised probe should read as "no", never as "yes".
    """

    default_user = None

    def __init__(self, *rules: tuple[str, int]):
        self.rules = rules
        self.commands: list[str] = []

    async def exec(self, command: str, **kwargs) -> _Result:
        self.commands.append(command)
        for needle, return_code in self.rules:
            if needle in command:
                return _Result(return_code)
        return _Result(1)


def _agent(cls, version: str | None = None):
    """An agent instance without Harbor's constructor, which wants a job."""
    agent = cls.__new__(cls)
    agent.logger = logging.getLogger("harbor_orchard.test")
    agent._version = version
    return agent


def _payload_works(tool: str) -> tuple[str, int]:
    return (f"{TOOLS_DIR}/bin/{tool}", 0)


#: Writing a shim has to succeed, or `exec_as_root` raises and the skip path
#: never completes.
SHIM_WRITE = ("mkdir -p /usr/local/bin", 0)


def _adopting(tool: str) -> _FakeEnvironment:
    """An environment where the payload runs and shims can be written."""
    return _FakeEnvironment(_payload_works(tool), SHIM_WRITE)


MUSL = ("alpine-release", 0)
GLIBC = ("alpine-release", 1)


@pytest.fixture
def no_stock_install(monkeypatch):
    """Record whether Harbor's own installer was reached."""
    calls: list[str] = []

    async def record_pi(self, environment):
        calls.append("pi")

    async def record_mini(self, environment):
        calls.append("mini")

    monkeypatch.setattr(StockPi, "install", record_pi)
    monkeypatch.setattr(StockMini, "install", record_mini)
    return calls


class TestPayloadIsUsed:
    @pytest.mark.asyncio
    async def test_a_working_pi_skips_the_nvm_install(self, no_stock_install):
        environment = _adopting("pi")

        await _agent(Pi).install(environment)

        assert no_stock_install == []
        # The probe has to *run* pi: `command -v` cannot tell a glibc binary on
        # a musl host from a working one.
        assert f"{TOOLS_DIR}/bin/pi" in environment.commands[0]
        assert "--version" in environment.commands[0]

    @pytest.mark.asyncio
    async def test_a_working_mini_skips_uv(self, no_stock_install):
        environment = _adopting("mini")

        await _agent(MiniSweAgent).install(environment)

        assert no_stock_install == []
        assert not any("uv tool install" in c for c in environment.commands)

    @pytest.mark.asyncio
    async def test_mini_is_published_under_the_name_harbor_runs(
        self, no_stock_install
    ):
        # Harbor's run command shells out to `mini-swe-agent`; the payload
        # publishes the wrapper as `mini`. Without the shim the install looks
        # like it succeeded and the run phase dies with 127.
        environment = _adopting("mini")

        await _agent(MiniSweAgent).install(environment)

        shims = [
            command
            for command in environment.commands
            if "/usr/local/bin/mini-swe-agent" in command
        ]
        assert len(shims) == 1
        assert f"{TOOLS_DIR}/bin/mini" in shims[0]


class TestBusyboxGaps:
    """`stdbuf` is GNU coreutils; Alpine has busybox and does not carry it.

    Harbor ends pi's run with `| stdbuf -oL tee`, so on a musl image that stage
    exits 127 and `set -o pipefail` reports the whole trial as an agent failure
    — after the payload was adopted, the pod was paid for and the model was
    called. Skipping Harbor's installer is not enough on its own.
    """

    @pytest.mark.asyncio
    async def test_a_stdbuf_shim_is_written(self, no_stock_install):
        environment = _adopting("pi")

        await _agent(Pi).install(environment)

        written = [c for c in environment.commands if "/usr/local/bin/stdbuf" in c]
        assert len(written) == 1
        # Never shadow a real coreutils stdbuf on a glibc image.
        assert "command -v stdbuf" in written[0]

    @pytest.mark.asyncio
    async def test_mini_gets_it_too(self, no_stock_install):
        # It is a property of the image, not of the agent, so it belongs to
        # every agent that adopts the payload.
        environment = _adopting("mini")

        await _agent(MiniSweAgent).install(environment)

        assert any("/usr/local/bin/stdbuf" in c for c in environment.commands)
        assert any("/usr/local/bin/mini-swe-agent" in c for c in environment.commands)

    def test_the_shim_is_valid_posix_sh(self):
        subprocess.run(["sh", "-n"], input=STDBUF_SHIM, text=True, check=True)

    @pytest.mark.parametrize(
        "args",
        [
            ["-oL"],            # what Harbor actually passes
            ["-o", "L"],        # the separated spelling
            ["--output=L"],
            ["--output", "L"],
            ["-i0", "-oL", "-eL"],
            ["--"],
            [],                 # no flags at all
        ],
    )
    def test_the_shim_drops_the_flags_and_runs_the_command(self, args, tmp_path):
        shim = tmp_path / "stdbuf"
        shim.write_text(STDBUF_SHIM, encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

        done = subprocess.run(
            ["sh", str(shim), *args, "echo", "ran"],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "ran"


class TestPayloadCannotWork:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cls", [Pi, MiniSweAgent])
    async def test_a_musl_image_fails_with_its_own_cause(self, cls, no_stock_install):
        # Harbor's installer would fail here too, but for a reason that reads as
        # a network problem (a 404 from nodejs.org, classified as
        # NetworkConnectionError). Not running it keeps the cause legible.
        environment = _FakeEnvironment(MUSL)

        with pytest.raises(PayloadUnusableError, match="musl"):
            await _agent(cls).install(environment)

        assert no_stock_install == []

    @pytest.mark.asyncio
    async def test_the_message_names_the_way_out(self, no_stock_install):
        environment = _FakeEnvironment(MUSL)

        with pytest.raises(PayloadUnusableError) as raised:
            await _agent(Pi).install(environment)

        assert "swebench-pro-musl.txt" in str(raised.value)


class TestFallback:
    @pytest.mark.asyncio
    async def test_a_pinned_version_still_goes_through_harbor(
        self, no_stock_install
    ):
        # The payload ships whatever the tools image baked in. Running that
        # while a caller asked for a specific version would answer a different
        # question than the one asked, and say nothing about having done so.
        environment = _adopting("pi")

        await _agent(Pi, version="0.85.1").install(environment)

        assert no_stock_install == ["pi"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cls", "expected"), [(Pi, "pi"), (MiniSweAgent, "mini")]
    )
    async def test_a_glibc_image_without_a_payload_defers_to_harbor(
        self, cls, expected, no_stock_install
    ):
        # Nothing about this module should change how a plain Docker-backed
        # image behaves: no payload mounted, glibc, so Harbor installs as usual.
        environment = _FakeEnvironment(GLIBC)

        await _agent(cls).install(environment)

        assert no_stock_install == [expected]


class TestMuslDetection:
    @pytest.mark.asyncio
    async def test_ldd_alone_is_enough(self, no_stock_install):
        # A musl image built on something other than Alpine has no
        # /etc/alpine-release, and reading its absence as "glibc" would send the
        # trial into the nvm install this class exists to avoid.
        environment = _FakeEnvironment(("ldd --version", 0))

        with pytest.raises(PayloadUnusableError):
            await _agent(Pi).install(environment)

        assert no_stock_install == []


class TestVersionReporting:
    def test_mini_reads_the_version_the_payload_recorded(self):
        # Upstream asks `uv tool list`, which reports nothing once the uv
        # install is skipped, so every result would carry a blank version.
        command = _agent(MiniSweAgent).get_version_command()

        assert f"{TOOLS_DIR}/VERSIONS" in command
        assert "uv tool list" in command  # still the fallback

    def test_the_recorded_line_parses(self):
        # The tools image writes "mini     2.4.6"; Harbor's parse_version has
        # to find a version in it or the label is the whole line.
        agent = _agent(MiniSweAgent)
        assert agent.parse_version("mini     2.4.6") == "2.4.6"


class TestStepTimeout:
    """mini's per-command timeout is 120s, not the 30s of its bundled config.

    The setting rides on the ``config`` mapping Harbor writes to a file and
    layers over ``-c mini``, so it lands in ``environment.timeout`` without
    disturbing anything else the bundled config sets.
    """

    @staticmethod
    def _config(tmp_path, **kwargs) -> dict:
        agent = MiniSweAgent(logs_dir=tmp_path, model_name="openai/m", **kwargs)
        return yaml.safe_load(agent._config_yaml) if agent._config_yaml else {}

    def test_it_is_120s_by_default(self, tmp_path):
        assert self._config(tmp_path) == {"environment": {"timeout": 120}}

    def test_it_can_be_set_per_run(self, tmp_path):
        # `--ak step_timeout=600` arrives as an int: Harbor JSON-parses values.
        assert self._config(tmp_path, step_timeout=600)["environment"]["timeout"] == 600

    def test_zero_keeps_minis_own(self, tmp_path):
        assert self._config(tmp_path, step_timeout=0) == {}

    def test_a_config_that_sets_it_wins_and_keeps_the_rest(self, tmp_path):
        config = {"environment": {"timeout": 45}, "agent": {"step_limit": 250}}
        assert self._config(tmp_path, config=config) == config

    def test_a_config_without_it_gets_it(self, tmp_path):
        assert self._config(tmp_path, config={"agent": {"step_limit": 250}}) == {
            "agent": {"step_limit": 250},
            "environment": {"timeout": 120},
        }

    def test_a_config_file_is_left_alone(self, tmp_path):
        # Harbor takes a file or a mapping, never both, so there is nothing to
        # merge into: the file is the caller's whole config.
        path = tmp_path / "mini.yaml"
        path.write_text("agent:\n  step_limit: 250\n")
        assert self._config(tmp_path, config_file=str(path)) == {
            "agent": {"step_limit": 250}
        }


class TestTemperature:
    """``--ak temperature=T`` becomes a value litellm sends with every request."""

    @staticmethod
    def _config(tmp_path, **kwargs) -> dict:
        agent = MiniSweAgent(logs_dir=tmp_path, model_name="openai/m", **kwargs)
        return yaml.safe_load(agent._config_yaml) if agent._config_yaml else {}

    def test_none_is_sent_by_default(self, tmp_path):
        assert "model" not in self._config(tmp_path)

    def test_it_lands_in_model_kwargs_beside_the_step_timeout(self, tmp_path):
        assert self._config(tmp_path, temperature=0.9) == {
            "environment": {"timeout": 120},
            "model": {"model_kwargs": {"temperature": 0.9}},
        }

    def test_a_config_that_sets_it_wins_and_keeps_the_rest(self, tmp_path):
        config = {"model": {"model_kwargs": {"temperature": 0.7, "top_p": 0.95}}}
        result = self._config(tmp_path, temperature=0.9, config=config)
        assert result["model"] == config["model"]

    def test_it_cannot_be_combined_with_a_config_file(self, tmp_path):
        path = tmp_path / "mini.yaml"
        path.write_text("agent:\n  step_limit: 250\n")
        with pytest.raises(ValueError, match="config_file"):
            MiniSweAgent(
                logs_dir=tmp_path,
                model_name="openai/m",
                temperature=0.9,
                config_file=str(path),
            )


class TestAgentDeadlineShim:
    """The shim is where Harbor's agent deadline becomes real.

    Harbor enforces it with an ``asyncio.wait_for`` that cancels the await and
    leaves the CLI running in the pod. Publishing the CLI behind a ``timeout``
    is what makes it stop on its own, early enough that the exec returns
    normally with the transcript and diff intact.
    """

    @pytest.mark.asyncio
    async def test_mini_is_published_behind_a_deadline(self, no_stock_install):
        environment = _adopting("mini")

        await _agent(MiniSweAgent).install(environment)

        shim = next(
            c for c in environment.commands if "/usr/local/bin/mini-swe-agent" in c
        )
        assert "timeout -k" in shim
        assert "ORCHARD_AGENT_DEADLINE" in shim
        assert f"{TOOLS_DIR}/bin/mini" in shim

    @pytest.mark.asyncio
    async def test_pi_is_published_behind_a_deadline(self, no_stock_install):
        # pi is the one agent Harbor invokes by the payload's own name, so
        # before this there was nothing of ours in front of it to bound.
        environment = _adopting("pi")

        await _agent(Pi).install(environment)

        shim = next(c for c in environment.commands if "/usr/local/bin/pi" in c)
        assert "timeout -k" in shim
        assert f"{TOOLS_DIR}/bin/pi" in shim

    @pytest.mark.asyncio
    async def test_no_shim_is_written_when_the_payload_is_not_adopted(
        self, no_stock_install
    ):
        # A pinned version routes through Harbor's installer, which installs
        # somewhere this shim has no business pointing at.
        environment = _adopting("mini")

        await _agent(MiniSweAgent, version="2.4.6").install(environment)

        assert not any("/usr/local/bin/mini-swe-agent" in c for c in environment.commands)


class _StubBase:
    """Stands in for Harbor's agent class under :class:`_ModelPatchCapture`."""

    async def run(self, instruction, environment, context) -> None:
        self.ran = True
        if self.fail is not None:
            raise self.fail


class _CaptureAgent(_ModelPatchCapture, _StubBase):
    def __init__(self, *, fail=None, capture_fails=False):
        self.logger = logging.getLogger("harbor_orchard.test")
        self.fail = fail
        self.ran = False
        self.capture_fails = capture_fails
        self.captures: list[tuple[str, int | None]] = []

    async def exec_as_root(self, environment, command, timeout_sec=None, **kwargs):
        if self.capture_fails:
            raise RuntimeError("the exec stream closed")
        self.captures.append((command, timeout_sec))
        result = _Result(0)
        result.stdout = "harbor-orchard: model.patch bytes: 1234 (/app)"
        return result


async def _run(agent: _CaptureAgent) -> None:
    await agent.run("solve it", _FakeEnvironment(), object())


class TestModelPatchCapture:
    """Why every agent here writes ``/logs/agent/model.patch``.

    A SWE-bench Pro V2 run is scored twice: once in the agent's own container,
    and once — the number the protocol says to report — by replaying this file
    into a *fresh* image. Upstream's ``patch_replay:PatchReplayAgent`` globs
    ``<source_job>/instance_*/agent/model.patch`` and scores the trial zero
    when it is missing, so a capture that silently does not happen does not
    look like a bug, it looks like a model that solved nothing.
    """

    @pytest.mark.asyncio
    async def test_the_patch_is_written_where_the_replay_looks_for_it(self):
        agent = _CaptureAgent()

        await _run(agent)

        assert agent.ran
        command, timeout = agent.captures[0][0], agent.captures[0][1]
        assert MODEL_PATCH_PATH in command
        assert timeout == MODEL_PATCH_TIMEOUT_S

    @pytest.mark.asyncio
    async def test_a_timed_out_rollout_is_still_captured(self):
        # The rollouts most worth re-grading include the ones that ran out of
        # budget with a complete fix already written: on the 2026-09-21 V1
        # sweep, half of every solved trial had timed out. Capturing only on
        # the success path would drop exactly those.
        agent = _CaptureAgent(fail=TimeoutError("agent execution timed out"))

        with pytest.raises(TimeoutError):
            await _run(agent)

        assert agent.captures, "no patch captured for a timed-out rollout"

    @pytest.mark.asyncio
    async def test_a_failing_capture_does_not_fail_the_trial(self):
        # This runs after the work is done. Letting it raise would convert a
        # finished rollout into a failed one, which costs far more than the
        # re-grade it was trying to enable.
        agent = _CaptureAgent(capture_fails=True)

        await _run(agent)

        assert agent.ran

    @pytest.mark.asyncio
    async def test_the_index_is_restored_for_the_shared_verifier(self):
        # `git add -A` is how new and deleted files reach the diff, but on a
        # shared-verifier task the grader runs next in this same container and
        # has to see the tree the agent left.
        agent = _CaptureAgent()

        await _run(agent)

        command = agent.captures[0][0]
        assert command.index("add -A") < command.index("reset -q")

    @pytest.mark.asyncio
    async def test_the_diff_is_taken_against_head(self):
        # DeepSWE's collect hook reads `git diff <base> HEAD`, which returns an
        # empty patch for an agent that never commits — and none of the agents
        # here commit. The images are checked out at the base commit, so the
        # index against HEAD is the whole of the agent's work.
        agent = _CaptureAgent()

        await _run(agent)

        command = agent.captures[0][0]
        assert "diff --cached" in command
        assert "HEAD" not in command

    @pytest.mark.asyncio
    async def test_both_repository_layouts_are_searched(self):
        # Most Pro images put the checkout at /app; a handful use /testbed.
        agent = _CaptureAgent()

        await _run(agent)

        command = agent.captures[0][0]
        assert "/app" in command and "/testbed" in command


class _GatedAgent(_CleanTreeGate, _ModelPatchCapture, _StubBase):
    """The published mixin stack: the gate, the capture, then Harbor's agent."""

    def __init__(self, report: str, *, check_fails: bool = False):
        self.logger = logging.getLogger("harbor_orchard.test")
        self.report = report
        self.check_fails = check_fails
        self.fail = None
        self.ran = False
        self.commands: list[tuple[str, int | None]] = []

    async def exec_as_root(self, environment, command, timeout_sec=None, **kwargs):
        self.commands.append((command, timeout_sec))
        # `--porcelain` is the pre-flight; anything else is the patch capture.
        if "--porcelain" not in command:
            result = _Result(0)
            result.stdout = "harbor-orchard: model.patch bytes: 0 (/app)"
            return result
        if self.check_fails:
            raise RuntimeError("the exec stream closed")
        result = _Result(0)
        result.stdout = self.report
        return result

    @property
    def checks(self) -> list[tuple[str, int | None]]:
        return [c for c in self.commands if "--porcelain" in c[0]]

    @property
    def captures(self) -> list[tuple[str, int | None]]:
        return [c for c in self.commands if "--porcelain" not in c[0]]


CLEAN = f"{CLEAN_TREE_PREFIX}clean (/app)"
UNKNOWN = f"{CLEAN_TREE_PREFIX}unknown: no git checkout under /app or /testbed"
DIRTY = (
    f"{CLEAN_TREE_PREFIX}dirty (/app): 2 tracked file(s) already modified\n"
    " M scanner/redhatbase.go\n M scanner/redhatbase_test.go"
)


class TestCleanTreeGate:
    """Why a modified checkout ends the trial instead of being scored.

    On the 2026-09-24 V2 sweep 96 trials opened a repository whose *task's own
    gold-patch targets* were already written — a re-submitted ``POST /exec``
    had left an earlier agent running and started a second one on its work.
    Those trials scored like any other, so the run reported a number that mixed
    two protocols. The gate makes that arrive as a failed trial instead.
    """

    @pytest.mark.asyncio
    async def test_a_dirty_tree_stops_the_trial_before_the_agent_runs(self):
        agent = _GatedAgent(DIRTY)

        with pytest.raises(DirtyWorkingTreeError):
            await _run(agent)

        assert not agent.ran, "the agent ran on someone else's work"

    @pytest.mark.asyncio
    async def test_the_failure_names_what_was_already_modified(self):
        # The paths are the diagnosis: on the V2 sweep they matched the task's
        # gold patch every time, which is what ruled out build noise.
        agent = _GatedAgent(DIRTY)

        with pytest.raises(DirtyWorkingTreeError, match="redhatbase.go"):
            await _run(agent)

    @pytest.mark.asyncio
    async def test_a_refused_trial_captures_no_patch(self):
        # There is no rollout to re-grade, and a model.patch of another agent's
        # work would be re-graded as this one's.
        agent = _GatedAgent(DIRTY)

        with pytest.raises(DirtyWorkingTreeError):
            await _run(agent)

        assert agent.captures == []

    @pytest.mark.asyncio
    async def test_a_clean_tree_runs_the_agent(self):
        agent = _GatedAgent(CLEAN)

        await _run(agent)

        assert agent.ran
        assert agent.checks[0][1] == CLEAN_TREE_TIMEOUT_S

    @pytest.mark.asyncio
    @pytest.mark.parametrize("report", [UNKNOWN, "", "something else entirely"])
    async def test_an_inconclusive_check_runs_the_agent(self, report):
        # A dataset that is not a git checkout, an image with no git, a probe
        # that printed nothing: none of these is evidence of a used pod, and
        # failing rollouts over them would cost more than the guard saves.
        agent = _GatedAgent(report)

        await _run(agent)

        assert agent.ran

    @pytest.mark.asyncio
    async def test_a_failing_check_runs_the_agent(self):
        agent = _GatedAgent(CLEAN, check_fails=True)

        await _run(agent)

        assert agent.ran

    @pytest.mark.asyncio
    async def test_the_check_can_be_switched_off(self, monkeypatch):
        # For a dataset whose images ship modifications deliberately. Off means
        # not asked at all, so it costs nothing on a run that does not want it.
        monkeypatch.setenv(REQUIRE_CLEAN_TREE_ENV_VAR, "0")
        agent = _GatedAgent(DIRTY)

        await _run(agent)

        assert agent.ran
        assert agent.checks == []


class TestEveryAgentCaptures:
    """The alias table decides which class runs, so all four have to capture.

    codex is the one that is easy to miss: it needs no install help, so it had
    no subclass here at all, and a re-grade of a codex job would have scored
    0.00% across the board with nothing in the logs saying why.
    """

    @pytest.mark.parametrize("cls", [Pi, MiniSweAgent, ClaudeCode, Codex])
    def test_the_capture_is_ahead_of_harbors_agent(self, cls):
        mro = cls.__mro__
        assert _ModelPatchCapture in mro
        harbor_class = next(c for c in mro if c.__module__.startswith("harbor."))
        assert mro.index(_ModelPatchCapture) < mro.index(harbor_class)

    @pytest.mark.parametrize("cls", [Pi, MiniSweAgent, ClaudeCode, Codex])
    def test_the_gate_is_ahead_of_the_capture(self, cls):
        # Ahead of Harbor's agent so it can stop the trial, and ahead of the
        # capture so a refused trial leaves no patch behind.
        mro = cls.__mro__
        assert _CleanTreeGate in mro
        assert mro.index(_CleanTreeGate) < mro.index(_ModelPatchCapture)

    def test_codex_keeps_harbors_name(self):
        # Results, alias tables and the run scripts all say "codex"; this
        # subclass exists for the patch capture and nothing else.
        assert Codex.name() == "codex"
