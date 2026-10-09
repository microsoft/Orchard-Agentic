"""EvalSandbox: repository operations, patch extraction, and the sync bridge."""

import asyncio

import pytest

from orchard_evalkit.jobs import ExecDispatchError
from orchard_evalkit.sandbox import (
    EvalSandbox,
    SandboxCommandError,
    SandboxGoneError,
    SandboxUnusableError,
    is_sandbox_failure,
)
from tests.fakes import (
    FakeJobClient,
    FakeJobResult,
    FakeResponseError,
    FakeSandboxInstance,
)


def make_sandbox(responder=None, **kwargs) -> tuple[EvalSandbox, FakeSandboxInstance]:
    instance = FakeSandboxInstance(responder=responder)
    sandbox = EvalSandbox(instance, workdir="/testbed", **kwargs)
    return sandbox, instance


class TestExec:
    @pytest.mark.asyncio
    async def test_output_combines_stdout_and_stderr(self):
        sandbox, _ = make_sandbox(
            lambda cmd: FakeJobResult(stdout="out", stderr="err", exit_code=0)
        )
        result = await sandbox.exec("echo hi")
        assert result.output == "outerr"
        assert result.succeeded

    @pytest.mark.asyncio
    async def test_failure_is_returned_not_raised_by_default(self):
        sandbox, _ = make_sandbox(lambda cmd: FakeJobResult(exit_code=1))
        result = await sandbox.exec("false")
        assert not result.succeeded
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_check_turns_failure_into_an_exception(self):
        sandbox, _ = make_sandbox(lambda cmd: FakeJobResult(exit_code=2, stderr="boom"))
        with pytest.raises(SandboxCommandError):
            await sandbox.exec("false", check=True)

    @pytest.mark.asyncio
    async def test_workdir_is_the_default_cwd(self):
        sandbox, instance = make_sandbox()
        await sandbox.exec("pwd")
        assert instance.commands[0]["cwd"] == "/testbed"

    @pytest.mark.asyncio
    async def test_merge_stderr_redirects_inside_the_sandbox(self):
        # Joining the two pipes here would put all of stderr after all of
        # stdout; the redirect has to happen where the command runs.
        sandbox, instance = make_sandbox()
        await sandbox.exec("make", merge_stderr=True)
        assert instance.commands[0]["command"] == "{\nmake\n} 2>&1"


class TestVanishedSandbox:
    """A pod reaped mid-run must be distinguishable from a failed command."""

    def _gone(self, status: int):
        def responder(cmd):
            raise FakeResponseError(status)

        return responder

    @pytest.mark.asyncio
    async def test_exec_translates_404(self):
        sandbox, _ = make_sandbox(self._gone(404))
        with pytest.raises(SandboxGoneError, match="sbx-test"):
            await sandbox.exec("echo hi")

    @pytest.mark.asyncio
    async def test_other_http_errors_pass_through_unchanged(self):
        sandbox, _ = make_sandbox(self._gone(500))
        with pytest.raises(FakeResponseError):
            await sandbox.exec("echo hi")

    @pytest.mark.asyncio
    async def test_extract_patch_raises_rather_than_reporting_an_empty_diff(self):
        # Returning "" here would be graded as EMPTY_PATCH, silently turning an
        # infrastructure failure into a legitimate-looking zero.
        sandbox, _ = make_sandbox(self._gone(404))
        with pytest.raises(SandboxGoneError):
            await sandbox.extract_patch("abc123")

    @pytest.mark.asyncio
    async def test_file_transfer_translates_404_too(self):
        sandbox, instance = make_sandbox()

        async def gone(*args, **kwargs):
            raise FakeResponseError(404)

        instance.upload_content = gone
        instance.download_content = gone
        with pytest.raises(SandboxGoneError):
            await sandbox.write_file("x", "/tmp/x")
        with pytest.raises(SandboxGoneError):
            await sandbox.read_file("/tmp/x")


class TestSendOnce:
    """A sandbox that failed under a command gets no second one.

    The SDK's ``exec`` re-sent a command whose connection dropped, which on one
    SWE-bench Pro V2 run started a second agent CLI in 297 of 613 trials, on
    the tree the first had already edited. The runner now hands every sandbox
    a job client that sends once and raises instead, and these pin what
    happens on this side: the failure reads as the cluster's, so the runner
    retries the instance in a fresh pod, and this pod is sent nothing more.
    """

    @pytest.mark.asyncio
    async def test_commands_go_through_the_job_client(self):
        instance = FakeSandboxInstance()
        sandbox = EvalSandbox(
            instance,
            workdir="/testbed",
            default_timeout=77,
            jobs=FakeJobClient(lambda: [instance]),
        )
        await sandbox.exec("echo hi")
        [call] = instance.commands
        assert call["command"] == "echo hi"
        assert call["cwd"] == "/testbed"
        assert call["timeout"] == 77

    @pytest.mark.asyncio
    async def test_a_dropped_command_is_the_clusters_failure(self):
        def responder(cmd):
            raise ExecDispatchError("POST failed after it was sent")

        sandbox, _ = make_sandbox(responder)
        with pytest.raises(ExecDispatchError) as caught:
            await sandbox.exec("mini --task ...")
        assert is_sandbox_failure(caught.value)

    @pytest.mark.asyncio
    async def test_nothing_more_is_sent_to_a_sandbox_that_failed(self):
        # A harness still reads the diff after a failed agent run; in this pod
        # that would queue behind the orphaned agent.
        def responder(cmd):
            if "mini" in cmd:
                raise ExecDispatchError("dropped")
            return FakeJobResult()

        sandbox, instance = make_sandbox(responder)
        with pytest.raises(ExecDispatchError):
            await sandbox.exec("mini --task ...")
        with pytest.raises(SandboxUnusableError, match="ExecDispatchError"):
            await sandbox.extract_patch("abc123")
        assert len(instance.commands) == 1

    @pytest.mark.asyncio
    async def test_a_command_that_never_reached_the_pod_is_the_clusters_failure(self):
        sandbox, _ = make_sandbox(
            lambda cmd: FakeJobResult(
                exit_code=None, status="failed", error="No pod IP available"
            )
        )
        with pytest.raises(SandboxUnusableError, match="No pod IP") as caught:
            await sandbox.exec("mini --task ...")
        assert is_sandbox_failure(caught.value)

    @pytest.mark.asyncio
    async def test_a_command_timeout_is_still_a_result(self):
        sandbox, instance = make_sandbox(
            lambda cmd: FakeJobResult(
                exit_code=None, status="failed", error="Execution timed out after 300s"
            )
        )
        result = await sandbox.exec("sleep 9999")
        assert result.exit_code == -1
        # The diff a timed-out agent left is still read.
        await sandbox.exec("git diff")
        assert len(instance.commands) == 2

    @pytest.mark.asyncio
    async def test_a_client_side_deadline_does_not_condemn_the_pod(self):
        def responder(cmd):
            if "mini" in cmd:
                raise TimeoutError("Job j1 did not complete within 300s")
            return FakeJobResult()

        sandbox, instance = make_sandbox(responder)
        with pytest.raises(TimeoutError):
            await sandbox.exec("mini --task ...")
        await sandbox.exec("git diff")
        assert len(instance.commands) == 2


class TestExecSync:
    @pytest.mark.asyncio
    async def test_bridges_from_a_worker_thread(self):
        # This is the path mini-swe-agent takes: a blocking call in a thread,
        # marshalled back onto the runner's loop.
        sandbox, _ = make_sandbox(lambda cmd: FakeJobResult(stdout="from-thread"))
        sandbox._loop = asyncio.get_running_loop()

        result = await asyncio.to_thread(lambda: sandbox.exec_sync("echo hi"))
        assert result.output == "from-thread"

    @pytest.mark.asyncio
    async def test_calling_on_the_loop_thread_is_rejected(self):
        # Doing this would deadlock, so it must fail loudly instead.
        sandbox, _ = make_sandbox()
        sandbox._loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="event loop thread"):
            sandbox.exec_sync("echo hi")

    def test_construction_off_loop_is_allowed(self):
        # Only exec_sync needs a loop; `await exec()` does not, and a sandbox
        # built in sync code (tests, scripts) must not blow up at construction.
        sandbox, _ = make_sandbox()
        assert sandbox._loop is None

    def test_exec_sync_without_a_loop_explains_itself(self):
        sandbox, _ = make_sandbox()
        with pytest.raises(RuntimeError, match="needs the event loop"):
            sandbox.exec_sync("echo hi")


class TestRepoOperations:
    @pytest.mark.asyncio
    async def test_prepare_repo_marks_the_tree_safe_and_resets_it(self):
        sandbox, instance = make_sandbox()
        await sandbox.prepare_repo("abc123")
        commands = [c["command"] for c in instance.commands]
        assert any("safe.directory" in c for c in commands)
        assert any("git checkout -f abc123" in c for c in commands)
        assert any("git clean -fd" in c for c in commands)

    @pytest.mark.asyncio
    async def test_prepare_repo_never_removes_ignored_files(self):
        # `-x` would delete the prebuilt environment: compiled extensions,
        # generated version files, *.egg-info. Every test would then error.
        sandbox, instance = make_sandbox()
        await sandbox.prepare_repo("abc123")
        assert not any("clean -fdx" in c["command"] for c in instance.commands)

    @pytest.mark.asyncio
    async def test_prepare_repo_without_a_base_commit_only_marks_safe(self):
        sandbox, instance = make_sandbox()
        await sandbox.prepare_repo("")
        assert len(instance.commands) == 1

    @pytest.mark.asyncio
    async def test_prepare_repo_can_leave_the_working_tree_alone(self):
        # The grading pod needs the image's uncommitted setup intact — sphinx's
        # `-rA` sed on tox.ini is what makes the log parseable at all.
        sandbox, instance = make_sandbox()
        await sandbox.prepare_repo("abc123", reset=False)
        commands = [c["command"] for c in instance.commands]
        assert commands == [c for c in commands if "safe.directory" in c]

    @pytest.mark.asyncio
    async def test_extract_patch_stages_new_files_and_excludes_scratch(self):
        captured = {}

        def responder(cmd):
            if "git diff" in cmd:
                captured["cmd"] = cmd
                return FakeJobResult(stdout="diff --git a/x b/x\n")
            return FakeJobResult()

        sandbox, _ = make_sandbox(responder)
        patch = await sandbox.extract_patch("abc123")

        assert patch == "diff --git a/x b/x\n"
        # `git add -A` is what makes newly created source files appear.
        assert "git add -A" in captured["cmd"]
        assert "--cached abc123" in captured["cmd"]
        # Agent scratch must never end up in the graded patch.
        assert "':(exclude)patch.txt'" in captured["cmd"]

    @pytest.mark.asyncio
    async def test_extract_patch_returns_empty_when_git_fails(self):
        sandbox, _ = make_sandbox(lambda cmd: FakeJobResult(exit_code=1, stderr="bad"))
        assert await sandbox.extract_patch("abc") == ""

    @pytest.mark.asyncio
    async def test_apply_patch_uploads_and_stops_at_the_first_success(self):
        sandbox, instance = make_sandbox()
        result = await sandbox.apply_patch("diff --git a/x b/x")

        assert result.succeeded
        assert "/tmp/orchard_eval_model.patch" in instance.files
        # A trailing newline is added; git apply rejects patches without one.
        assert instance.files["/tmp/orchard_eval_model.patch"].endswith(b"\n")
        commands = [c["command"] for c in instance.commands]
        assert len(commands) == 1
        assert "git apply --verbose" in commands[0]

    @pytest.mark.asyncio
    async def test_apply_patch_resets_the_tree_between_attempts(self):
        # swebench does this because a failed attempt (--reject above all)
        # leaves partial state that makes every later attempt fail too.
        sandbox, instance = make_sandbox(
            lambda cmd: FakeJobResult(
                exit_code=0 if ("clean -fd" in cmd or "--reverse" in cmd) else 1
            )
        )
        result = await sandbox.apply_patch("diff --git a/x b/x")

        commands = [c["command"] for c in instance.commands]
        applies = [c for c in commands if "orchard_eval_model.patch" in c]
        assert len(applies) == 5  # 4 apply commands + the reverse check
        assert "--3way" in applies[1]
        assert "--reject" in applies[2]
        assert "patch --batch --forward --fuzz=5" in applies[3]
        assert sum("git checkout -- . ; git clean -fd" in c for c in commands) == 3
        # The reverse check succeeded, so the patch was already applied.
        assert result.succeeded

    @pytest.mark.asyncio
    async def test_apply_patch_fails_when_nothing_applies(self):
        sandbox, _ = make_sandbox(lambda cmd: FakeJobResult(exit_code=1))
        result = await sandbox.apply_patch("diff --git a/x b/x")
        assert not result.succeeded


class TestProbeHarness:
    @pytest.mark.asyncio
    async def test_returns_the_resolved_path(self):
        sandbox, _ = make_sandbox(
            lambda cmd: FakeJobResult(stdout="/opt/sandbox-tools/bin/codex\n")
        )
        assert await sandbox.probe_harness("codex") == "/opt/sandbox-tools/bin/codex"

    @pytest.mark.asyncio
    async def test_returns_none_when_absent(self):
        sandbox, _ = make_sandbox(lambda cmd: FakeJobResult(stdout="", exit_code=1))
        assert await sandbox.probe_harness("nope") is None
