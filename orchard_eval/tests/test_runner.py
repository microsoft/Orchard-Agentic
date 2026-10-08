"""Runner orchestration: resume, retries, cleanup, and failure containment."""

import json

import pytest

from orchard_evalkit import runner as runner_module
from orchard_evalkit.config import RunConfig
from orchard_evalkit.datasets.swebench import instances_from_records
from orchard_evalkit.harnesses.base import Harness, register_harness
from orchard_evalkit.models import (
    EXIT_COMPLETED,
    EXIT_INFRA_ERROR,
    EXIT_TIMEOUT,
    GradeResult,
    InstanceRecord,
    RolloutResult,
)
from orchard_evalkit.runner import EvalRunner, ResultWriter, load_existing_records
from orchard_evalkit.sandbox import SandboxGoneError
from tests.fakes import FakeJobClient, FakeSandboxClient


@register_harness
class _AlwaysPatchesHarness(Harness):
    name = "test-always-patches"
    description = "test double that always returns a patch"

    async def rollout(self, ctx):
        return RolloutResult(
            patch="diff --git a/f b/f\n",
            exit_status=EXIT_COMPLETED,
            messages=[
                {"role": "assistant", "content": "looking", "extra": {}},
                {"role": "tool", "content": "ok", "extra": {}},
            ],
            trajectory_extra={"format": "demo"},
            metrics={"cost": 0.25},
        )


@register_harness
class _SilentHarness(Harness):
    """A harness that captures no trajectory at all — the failure we guard."""

    name = "test-silent"
    description = "test double that returns no trajectory"

    async def rollout(self, ctx):
        return RolloutResult(patch="diff --git a/f b/f\n")


@register_harness
class _CrashingHarness(Harness):
    name = "test-crashing"
    description = "test double that raises"

    async def rollout(self, ctx):
        raise RuntimeError("harness exploded")


@register_harness
class _EmptyPatchHarness(Harness):
    name = "test-empty-patch"
    description = "test double that produces nothing to grade"

    async def rollout(self, ctx):
        return RolloutResult(patch="")


@register_harness
class _FlakySandboxHarness(Harness):
    """Loses its pod for the first ``fail_times`` attempts, then succeeds."""

    name = "test-flaky-sandbox"
    description = "test double whose sandbox is reaped mid-rollout"
    fail_times = 0
    calls = 0

    async def rollout(self, ctx):
        type(self).calls += 1
        if type(self).calls <= type(self).fail_times:
            return RolloutResult(
                exit_status=EXIT_INFRA_ERROR,
                error="sandbox sbx-1 no longer exists (404)",
            )
        return RolloutResult(patch="diff --git a/f b/f\n", exit_status=EXIT_COMPLETED)


@register_harness
class _TimingOutHarness(Harness):
    """Spends its whole budget for the first ``fail_times`` attempts."""

    name = "test-timeout"
    description = "test double that hits its deadline"
    fail_times = 99
    calls = 0

    async def rollout(self, ctx):
        type(self).calls += 1
        if type(self).calls <= type(self).fail_times:
            return RolloutResult(
                exit_status=EXIT_TIMEOUT,
                error="agent hit its 1800s deadline",
            )
        return RolloutResult(patch="diff --git a/f b/f\n", exit_status=EXIT_COMPLETED)


@register_harness
class _RaisesSandboxGoneHarness(Harness):
    """Lets SandboxGoneError propagate, as harnesses that don't handle it do."""

    name = "test-sandbox-gone"
    description = "test double that raises SandboxGoneError"

    async def rollout(self, ctx):
        raise SandboxGoneError("sbx-1", RuntimeError("404"))


class _ServerError(Exception):
    """Shaped like the SDK's 500, which carries the status on the exception."""

    status = 500


@register_harness
class _RaisesServerErrorHarness(Harness):
    """Hits an orchestrator 500 for the first ``fail_times`` attempts."""

    name = "test-server-error"
    description = "test double whose sandbox returns 500"
    fail_times = 0
    calls = 0

    async def rollout(self, ctx):
        type(self).calls += 1
        if type(self).calls <= type(self).fail_times:
            raise _ServerError("Internal Server Error")
        return RolloutResult(patch="diff --git a/f b/f\n", exit_status=EXIT_COMPLETED)


def _config(tmp_path, **kwargs) -> RunConfig:
    base = {
        "output_dir": tmp_path,
        "run_name": "t",
        "concurrency": 4,
        "harness": {"name": "test-always-patches"},
        "model": {"name": "test-model"},
        "grading": {"enabled": False},
        "save_artifacts": False,
    }
    base.update(kwargs)
    return RunConfig(**base)


def _instances(n: int):
    return instances_from_records(
        [
            {
                "instance_id": f"repo__proj-{i}",
                "problem_statement": "s",
                "base_commit": "c",
            }
            for i in range(n)
        ]
    )


@pytest.fixture
def fake_client(monkeypatch):
    """Swap AsyncSandboxClient for an in-memory fake.

    Each ``run()`` builds its own client, so pod-creation counts are tracked
    across all of them — otherwise a resume test would inspect the *previous*
    run's client and read a stale number.
    """
    state = {"clients": [], "fail_times": 0}

    def factory(**kwargs):
        client = FakeSandboxClient(**kwargs, fail_times=state["fail_times"])
        state["clients"].append(client)
        return client

    monkeypatch.setattr(runner_module, "AsyncSandboxClient", factory)

    state["created"] = lambda: [
        sb for client in state["clients"] for sb in client.created
    ]
    monkeypatch.setattr(
        runner_module, "JobClient", lambda *_, **__: FakeJobClient(state["created"])
    )
    state["create_calls"] = lambda: sum(c.create_calls for c in state["clients"])
    state["last_create_calls"] = lambda: (
        state["clients"][-1].create_calls if state["clients"] else 0
    )
    return state


class TestResultWriter:
    @pytest.mark.asyncio
    async def test_appends_one_json_object_per_line(self, tmp_path):
        path = tmp_path / "results.jsonl"
        writer = ResultWriter(path)
        await writer.append(InstanceRecord(instance_id="a", harness="h"))
        await writer.append(InstanceRecord(instance_id="b", harness="h"))

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["instance_id"] == "b"


class TestLoadExistingRecords:
    def test_missing_file_is_empty(self, tmp_path):
        assert load_existing_records(tmp_path / "nope.jsonl") == {}

    def test_a_torn_line_does_not_block_resume(self, tmp_path):
        # An interrupted run can leave a half-written last line; losing the
        # whole results file to it would be far worse than skipping it.
        path = tmp_path / "results.jsonl"
        path.write_text(
            json.dumps({"instance_id": "a", "harness": "h"}) + "\n" + '{"broken'
        )
        records = load_existing_records(path)
        assert list(records) == ["a"]


class TestRunner:
    @pytest.mark.asyncio
    async def test_runs_every_instance_and_writes_results(self, tmp_path, fake_client):
        config = _config(tmp_path)
        records = await EvalRunner(config).run(_instances(5))

        assert len(records) == 5
        assert all(r.patch for r in records)
        assert len(load_existing_records(config.results_path)) == 5

    @pytest.mark.asyncio
    async def test_sandboxes_are_deleted(self, tmp_path, fake_client):
        await EvalRunner(_config(tmp_path)).run(_instances(3))
        assert all(sb.deleted for sb in fake_client["created"]())

    @pytest.mark.asyncio
    async def test_keep_sandbox_skips_deletion(self, tmp_path, fake_client):
        await EvalRunner(_config(tmp_path, keep_sandbox=True)).run(_instances(2))
        assert not any(sb.deleted for sb in fake_client["created"]())

    @pytest.mark.asyncio
    async def test_resume_skips_recorded_instances(self, tmp_path, fake_client):
        config = _config(tmp_path)
        await EvalRunner(config).run(_instances(3))
        assert fake_client["create_calls"]() == 3

        # A second run over the same instances must create no new sandboxes.
        records = await EvalRunner(config).run(_instances(3))
        assert len(records) == 3
        assert fake_client["create_calls"]() == 3

    @pytest.mark.asyncio
    async def test_no_resume_reruns_everything(self, tmp_path, fake_client):
        config = _config(tmp_path)
        await EvalRunner(config).run(_instances(2))
        rerun_config = _config(tmp_path, resume=False)
        await EvalRunner(rerun_config).run(_instances(2))
        # 2 from the first run plus 2 from the forced re-run.
        assert fake_client["create_calls"]() == 4

    @pytest.mark.asyncio
    async def test_resume_reattempts_infra_failures(self, tmp_path, fake_client):
        # An infra_error carries no score, so keeping it would let one reaped
        # pod permanently remove an instance from the benchmark.
        config = _config(tmp_path)
        config.results_path.parent.mkdir(parents=True, exist_ok=True)
        config.results_path.write_text(
            json.dumps(
                {
                    "instance_id": "repo__proj-0",
                    "harness": "test-always-patches",
                    "exit_status": EXIT_INFRA_ERROR,
                }
            )
            + "\n"
        )

        records = await EvalRunner(config).run(_instances(1))
        assert fake_client["create_calls"]() == 1
        assert [r.exit_status for r in records] == [EXIT_COMPLETED]

    @pytest.mark.asyncio
    async def test_resume_keeps_infra_failures_when_asked(self, tmp_path, fake_client):
        config = _config(tmp_path, retry_infra_on_resume=False)
        config.results_path.parent.mkdir(parents=True, exist_ok=True)
        config.results_path.write_text(
            json.dumps(
                {
                    "instance_id": "repo__proj-0",
                    "harness": "test-always-patches",
                    "exit_status": EXIT_INFRA_ERROR,
                }
            )
            + "\n"
        )

        records = await EvalRunner(config).run(_instances(1))
        assert fake_client["create_calls"]() == 0
        assert [r.exit_status for r in records] == [EXIT_INFRA_ERROR]

    @pytest.mark.asyncio
    async def test_pod_creation_is_retried(self, tmp_path, fake_client, monkeypatch):
        monkeypatch.setattr(runner_module.asyncio, "sleep", _no_sleep)
        fake_client["fail_times"] = 2
        config = _config(tmp_path, max_retries=2, concurrency=1)

        records = await EvalRunner(config).run(_instances(1))
        assert records[0].exit_status == EXIT_COMPLETED
        assert records[0].attempts == 3

    @pytest.mark.asyncio
    async def test_exhausted_retries_produce_an_infra_error_record(
        self, tmp_path, fake_client, monkeypatch
    ):
        monkeypatch.setattr(runner_module.asyncio, "sleep", _no_sleep)
        fake_client["fail_times"] = 99
        config = _config(tmp_path, max_retries=1, concurrency=1)

        records = await EvalRunner(config).run(_instances(1))
        assert records[0].exit_status == EXIT_INFRA_ERROR
        # The instance must still be recorded, not silently dropped.
        assert len(load_existing_records(config.results_path)) == 1

    @pytest.mark.asyncio
    async def test_a_crashing_harness_does_not_abort_the_run(
        self, tmp_path, fake_client
    ):
        config = _config(tmp_path, harness={"name": "test-crashing"})
        records = await EvalRunner(config).run(_instances(3))

        assert len(records) == 3
        assert all(r.exit_status == EXIT_INFRA_ERROR for r in records)
        assert all("harness exploded" in (r.error or "") for r in records)

    @pytest.mark.asyncio
    async def test_summary_and_predictions_are_written(self, tmp_path, fake_client):
        config = _config(tmp_path)
        await EvalRunner(config).run(_instances(2))

        summary = json.loads((config.run_dir / "summary.json").read_text())
        assert summary["total"] == 2

        preds = json.loads((config.run_dir / "preds.json").read_text())
        assert set(preds) == {"repo__proj-0", "repo__proj-1"}
        assert preds["repo__proj-0"]["model_name_or_path"] == "test-model"

    @pytest.mark.asyncio
    async def test_concurrency_is_bounded(self, tmp_path, fake_client):
        config = _config(tmp_path, concurrency=2)
        runner = EvalRunner(config)
        assert runner._semaphore._value == 2
        await runner.run(_instances(4))


class TestRolloutRetries:
    """An instance whose pod is reaped gets a fresh one, up to a bounded budget.

    The distinction that matters: a lost *sandbox* is the cluster's failure and
    is retried, while an agent that failed on a healthy pod is a result and must
    not be, or the score inflates.
    """

    @pytest.fixture(autouse=True)
    def _reset_harness(self, monkeypatch):
        monkeypatch.setattr(runner_module.asyncio, "sleep", _no_sleep)
        _FlakySandboxHarness.calls = 0
        _FlakySandboxHarness.fail_times = 0
        _TimingOutHarness.calls = 0
        _TimingOutHarness.fail_times = 99
        yield
        _FlakySandboxHarness.calls = 0
        _FlakySandboxHarness.fail_times = 0
        _TimingOutHarness.calls = 0
        _TimingOutHarness.fail_times = 99

    @pytest.mark.asyncio
    async def test_a_lost_sandbox_is_retried_on_a_new_pod(self, tmp_path, fake_client):
        _FlakySandboxHarness.fail_times = 2
        config = _config(
            tmp_path, harness={"name": "test-flaky-sandbox"}, rollout_retries=2
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].exit_status == EXIT_COMPLETED
        assert records[0].rollout_attempts == 3
        # A fresh pod per attempt: reusing the dead one is the whole problem.
        assert fake_client["create_calls"]() == 3

    @pytest.mark.asyncio
    async def test_the_budget_is_bounded(self, tmp_path, fake_client):
        _FlakySandboxHarness.fail_times = 99
        config = _config(
            tmp_path, harness={"name": "test-flaky-sandbox"}, rollout_retries=2
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].exit_status == EXIT_INFRA_ERROR
        assert records[0].rollout_attempts == 3
        assert fake_client["create_calls"]() == 3

    @pytest.mark.asyncio
    async def test_zero_retries_attempts_once(self, tmp_path, fake_client):
        _FlakySandboxHarness.fail_times = 99
        config = _config(
            tmp_path, harness={"name": "test-flaky-sandbox"}, rollout_retries=0
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].rollout_attempts == 1
        assert fake_client["create_calls"]() == 1

    @pytest.mark.asyncio
    async def test_a_deadline_hit_still_gets_a_spare_attempt(
        self, tmp_path, fake_client
    ):
        # "The agent used its whole budget" and "the pod died in the last
        # seconds of it" are indistinguishable from the runner, so a deadline
        # is not a dead end — losing an instance that would have scored to that
        # ambiguity costs more than one spare pod.
        _TimingOutHarness.fail_times = 1
        config = _config(
            tmp_path,
            harness={"name": "test-timeout"},
            rollout_retries=2,
            timeout_retries=1,
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].exit_status == EXIT_COMPLETED
        assert records[0].rollout_attempts == 2

    @pytest.mark.asyncio
    async def test_a_deadline_budget_is_smaller_than_the_infra_one(
        self, tmp_path, fake_client
    ):
        # The point of the split: an instance that really is too slow stops
        # after one spare pod instead of spending the full infra budget on
        # three half-hour attempts that all reach the same wall.
        _TimingOutHarness.fail_times = 99
        config = _config(
            tmp_path,
            harness={"name": "test-timeout"},
            rollout_retries=2,
            timeout_retries=1,
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].exit_status == EXIT_TIMEOUT
        assert records[0].rollout_attempts == 2
        assert fake_client["create_calls"]() == 2

    @pytest.mark.asyncio
    async def test_a_deadline_can_be_given_the_full_budget(self, tmp_path, fake_client):
        # The split is a default, not a policy: a run that would rather pay
        # for three attempts can still ask for them.
        _TimingOutHarness.fail_times = 99
        config = _config(
            tmp_path,
            harness={"name": "test-timeout"},
            rollout_retries=2,
            timeout_retries=2,
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].rollout_attempts == 3

    @pytest.mark.asyncio
    async def test_infra_retries_are_untouched_by_the_deadline_budget(
        self, tmp_path, fake_client
    ):
        # The catch-all has to stay a catch-all: an unrecognized cluster fault
        # still earns every pod rollout_retries allows, whatever the deadline
        # budget is set to.
        _FlakySandboxHarness.fail_times = 2
        config = _config(
            tmp_path,
            harness={"name": "test-flaky-sandbox"},
            rollout_retries=2,
            timeout_retries=0,
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].exit_status == EXIT_COMPLETED
        assert records[0].rollout_attempts == 3

    @pytest.mark.asyncio
    async def test_a_propagated_sandbox_gone_error_is_retried(
        self, tmp_path, fake_client
    ):
        config = _config(
            tmp_path, harness={"name": "test-sandbox-gone"}, rollout_retries=2
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].exit_status == EXIT_INFRA_ERROR
        assert records[0].rollout_attempts == 3
        assert "no longer exists" in (records[0].error or "")

    @pytest.mark.asyncio
    async def test_an_agent_failure_is_never_retried(self, tmp_path, fake_client):
        # An empty patch is a result. Retrying it would buy a second chance at
        # the same instance and quietly inflate the resolve rate.
        config = _config(
            tmp_path, harness={"name": "test-empty-patch"}, rollout_retries=2
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].rollout_attempts == 1
        assert fake_client["create_calls"]() == 1

    @pytest.mark.asyncio
    async def test_any_sandbox_failure_is_retried_not_only_a_404(
        self, tmp_path, fake_client, monkeypatch
    ):
        # A 500, a refused connection and a reaped pod are the same event as
        # far as the instance is concerned: the pod is unusable.
        monkeypatch.setattr(runner_module.asyncio, "sleep", _no_sleep)
        _RaisesServerErrorHarness.calls = 0
        _RaisesServerErrorHarness.fail_times = 2
        config = _config(
            tmp_path, harness={"name": "test-server-error"}, rollout_retries=2
        )

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].exit_status == EXIT_COMPLETED
        assert records[0].rollout_attempts == 3
        assert fake_client["create_calls"]() == 3

    @pytest.mark.asyncio
    async def test_a_harness_bug_is_not_mistaken_for_a_sandbox_failure(
        self, tmp_path, fake_client
    ):
        config = _config(tmp_path, harness={"name": "test-crashing"}, rollout_retries=2)

        records = await EvalRunner(config).run(_instances(1))

        assert fake_client["create_calls"]() == 1
        assert "harness exploded" in (records[0].error or "")

    @pytest.mark.asyncio
    async def test_exhausted_pod_creation_earns_a_whole_new_attempt(
        self, tmp_path, fake_client, monkeypatch
    ):
        # Pod creation used to fail the instance outright once its own cheap
        # retries ran out, without ever spending a whole-rollout attempt.
        monkeypatch.setattr(runner_module.asyncio, "sleep", _no_sleep)
        fake_client["fail_times"] = 3
        config = _config(tmp_path, max_retries=1, rollout_retries=2, concurrency=1)

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].exit_status == EXIT_COMPLETED
        assert records[0].rollout_attempts == 2

    @pytest.mark.asyncio
    async def test_a_lost_eval_pod_is_retried(self, tmp_path, fake_client, monkeypatch):
        # The rollout is already paid for by then, so losing the cheap pod must
        # not cost the expensive one.
        calls = {"n": 0}

        async def flaky_grade(sandbox, instance, patch, **_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SandboxGoneError("sbx-eval", RuntimeError("404"))
            return GradeResult(resolved=True, resolved_status="RESOLVED_FULL")

        monkeypatch.setattr(runner_module, "grade_swebench_patch", flaky_grade)
        config = _config(tmp_path, grading={"enabled": True}, rollout_retries=2)

        records = await EvalRunner(config).run(_instances(1))

        assert records[0].resolved
        assert calls["n"] == 2


class TestGradingIsolation:
    """Grading must never run in the pod the agent worked in.

    The agent can install packages, warm caches, or leave files outside the
    repository — none of which a patch carries. Scoring in the same pod would
    silently credit those, so upstream SWE-bench builds a container per
    prediction and so does this runner.
    """

    @pytest.mark.asyncio
    async def test_grading_gets_a_second_pod_after_the_first_is_deleted(
        self, tmp_path, fake_client, monkeypatch
    ):
        seen = {}

        async def fake_grade(sandbox, instance, patch, **_):
            seen["graded_in"] = sandbox.sandbox_id
            seen["rollout_pod_deleted"] = fake_client["created"]()[0].deleted
            return GradeResult(resolved=True, reward=1.0)

        monkeypatch.setattr(runner_module, "grade_swebench_patch", fake_grade)

        config = _config(tmp_path, grading={"enabled": True})
        records = await EvalRunner(config).run(_instances(1))

        rollout_pod, eval_pod = fake_client["created"]()
        assert seen["graded_in"] == eval_pod.sandbox_id
        assert seen["rollout_pod_deleted"] is True
        assert records[0].sandbox_id == rollout_pod.sandbox_id
        assert records[0].eval_sandbox_id == eval_pod.sandbox_id
        assert records[0].resolved

    @pytest.mark.asyncio
    async def test_both_pods_are_deleted(self, tmp_path, fake_client, monkeypatch):
        async def fake_grade(sandbox, instance, patch, **_):
            return GradeResult()

        monkeypatch.setattr(runner_module, "grade_swebench_patch", fake_grade)

        await EvalRunner(_config(tmp_path, grading={"enabled": True})).run(
            _instances(2)
        )
        created = fake_client["created"]()
        assert len(created) == 4
        assert all(sb.deleted for sb in created)

    @pytest.mark.asyncio
    async def test_an_empty_patch_costs_no_eval_pod(self, tmp_path, fake_client):
        config = _config(
            tmp_path,
            harness={"name": "test-empty-patch"},
            grading={"enabled": True},
        )
        records = await EvalRunner(config).run(_instances(1))

        assert fake_client["create_calls"]() == 1
        assert records[0].resolved_status == "EMPTY_PATCH"

    @pytest.mark.asyncio
    async def test_eval_pod_failure_is_recorded_not_raised(
        self, tmp_path, fake_client, monkeypatch
    ):
        # A cluster that cannot start the eval pod must not disappear into the
        # resolve rate looking like every other zero.
        monkeypatch.setattr(runner_module.asyncio, "sleep", _no_sleep)
        original = FakeSandboxClient.create_sandbox

        async def flaky_create(self, image, **kwargs):
            if self.create_calls >= 1:
                self.create_calls += 1
                raise RuntimeError("no capacity")
            return await original(self, image, **kwargs)

        monkeypatch.setattr(FakeSandboxClient, "create_sandbox", flaky_create)

        config = _config(tmp_path, grading={"enabled": True}, max_retries=1)
        records = await EvalRunner(config).run(_instances(1))

        assert records[0].resolved_status == "EVAL_INFRA_ERROR"
        assert records[0].exit_status == EXIT_COMPLETED
        assert "no capacity" in (records[0].error or "")


class TestTrajectoryPersistence:
    """The runner must save a trajectory for EVERY harness, not just the ones
    that remember to save their own. This is the guarantee, so it is tested at
    the runner level rather than per harness."""

    @pytest.mark.asyncio
    async def test_trajectory_is_written_for_any_harness(self, tmp_path, fake_client):
        config = _config(tmp_path, save_artifacts=True)
        await EvalRunner(config).run(_instances(1))

        traj_path = config.instances_dir / "repo__proj-0" / "trajectory.json"
        assert traj_path.exists()

        payload = json.loads(traj_path.read_text())
        assert payload["instance_id"] == "repo__proj-0"
        assert payload["harness"] == "test-always-patches"
        assert payload["n_messages"] == 2
        assert payload["messages"][0]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_a_silent_harness_still_gets_a_trajectory_file(
        self, tmp_path, fake_client
    ):
        # An empty trajectory must be distinguishable from a run that never
        # wrote one at all.
        config = _config(tmp_path, harness={"name": "test-silent"}, save_artifacts=True)
        await EvalRunner(config).run(_instances(1))

        payload = json.loads(
            (config.instances_dir / "repo__proj-0" / "trajectory.json").read_text()
        )
        assert payload["n_messages"] == 0
        assert payload["messages"] == []

    @pytest.mark.asyncio
    async def test_message_count_reaches_the_results_file(self, tmp_path, fake_client):
        # Visible in results.jsonl, so a run that stopped capturing is
        # detectable without walking the artifacts tree.
        config = _config(tmp_path, save_artifacts=True)
        records = await EvalRunner(config).run(_instances(1))
        assert records[0].n_messages == 2

        reloaded = load_existing_records(config.results_path)
        assert reloaded["repo__proj-0"].n_messages == 2

    @pytest.mark.asyncio
    async def test_missing_trajectories_surface_in_the_summary(
        self, tmp_path, fake_client
    ):
        config = _config(tmp_path, harness={"name": "test-silent"}, save_artifacts=True)
        await EvalRunner(config).run(_instances(3))

        summary = json.loads((config.run_dir / "summary.json").read_text())
        assert summary["missing_trajectories"] == 3
        assert summary["mean_messages"] == 0.0


async def _no_sleep(_seconds):
    return None
