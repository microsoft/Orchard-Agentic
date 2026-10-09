"""Concurrent evaluation runner.

One coroutine per instance, bounded by a semaphore. Each instance uses **two
pods in sequence**, never at the same time:

1. a *rollout* pod — created, prepared, handed to the agent, then **deleted**;
2. an *eval* pod — created fresh from the same image, given only the extracted
   patch, and used to run the benchmark's own tests.

The agent never shares an environment with the grader, so nothing it does
outside git — installing a package, editing a conftest, seeding a cache — can
reach the score. This is what upstream SWE-bench does, where evaluation always
starts a container of its own from the instance image.

Sandboxes are the expensive resource, so the concurrency limit is expressed in
sandboxes rather than in threads or processes.

Two properties matter more than throughput here:

* **a failed instance is a result, not a crash** — the only thing retried is an
  *infrastructure* failure (pod creation, a sandbox lost mid-instance). A failed
  agent, an unappliable patch, or a red test suite are outcomes and are recorded
  as such.
* **results are durable as they are produced** — each record is appended to
  ``results.jsonl`` immediately, so an interrupted run resumes without
  re-spending money on instances that already finished.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from orchard_env import AsyncSandboxClient

from orchard_evalkit.config import RunConfig
from orchard_evalkit.datasets import (
    BENCHMARK_SWEBENCH_PRO,
    benchmark_of,
    load_instances,
)
from orchard_evalkit.grading.swebench import grade_swebench_patch
from orchard_evalkit.grading.swebench_pro import (
    RunScriptStore,
    grade_swebench_pro_patch,
)
from orchard_evalkit.harnesses import Harness, RolloutContext, build_harness
from orchard_evalkit.jobs import JobClient
from orchard_evalkit.models import (
    EXIT_INFRA_ERROR,
    EXIT_TIMEOUT,
    GradeResult,
    InstanceRecord,
    RolloutResult,
    TaskInstance,
    Timings,
)
from orchard_evalkit.report import (
    format_duration,
    format_report,
    summarize,
    write_predictions,
    write_report,
    write_summary,
)
from orchard_evalkit.sandbox import EvalSandbox, is_sandbox_failure

logger = logging.getLogger(__name__)


class ResultWriter:
    """Append-only JSONL sink, safe for concurrent writers."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, record: InstanceRecord) -> None:
        line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()


def load_existing_records(path: Path) -> dict[str, InstanceRecord]:
    """Read prior results for resume. Malformed lines are skipped, not fatal."""
    if not path.exists():
        return {}
    records: dict[str, InstanceRecord] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = InstanceRecord(**json.loads(line))
        except Exception:  # noqa: BLE001 - a torn last line must not block resume
            logger.warning("Skipping unreadable line %d in %s", line_no, path)
            continue
        records[record.instance_id] = record
    return records


class EvalRunner:
    """Runs one :class:`RunConfig` to completion."""

    def __init__(self, config: RunConfig):
        self.config = config
        self.harness: Harness = build_harness(config.harness, config.model)
        self.writer = ResultWriter(config.results_path)
        #: Which benchmark's grading rules apply. Settled in :meth:`run`, once
        #: the rows are in hand — a local .jsonl identifies itself by its
        #: columns rather than by its name.
        self.benchmark = ""
        self._pro_scripts: RunScriptStore | None = None
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._done = 0
        self._resolved = 0
        self._total = 0
        self._progress_lock = asyncio.Lock()
        self._started_at = 0.0
        #: Runs every sandbox command of this run; built in :meth:`run`.
        self._jobs: JobClient | None = None

    # ------------------------------------------------------------------

    async def run(
        self, instances: list[TaskInstance] | None = None
    ) -> list[InstanceRecord]:
        cfg = self.config
        cfg.run_dir.mkdir(parents=True, exist_ok=True)
        self._started_at = time.monotonic()

        if instances is None:
            self.benchmark, instances = load_instances(cfg.dataset, cfg.sandbox)
        else:
            self.benchmark = benchmark_of(instances, cfg.dataset)
        if self.benchmark == BENCHMARK_SWEBENCH_PRO:
            self._pro_scripts = RunScriptStore(
                cfg.grading.pro_scripts_dir or None,
                cache_dir=cfg.grading.pro_scripts_cache or None,
                ref=cfg.grading.pro_scripts_ref,
            )

        existing = load_existing_records(cfg.results_path) if cfg.resume else {}
        if cfg.retry_infra_on_resume:
            retryable = [
                iid
                for iid, record in existing.items()
                if record.exit_status == EXIT_INFRA_ERROR
            ]
            for instance_id in retryable:
                del existing[instance_id]
            if retryable:
                logger.info(
                    "Re-running %d instance(s) previously recorded as %s",
                    len(retryable),
                    EXIT_INFRA_ERROR,
                )
        pending = [i for i in instances if i.instance_id not in existing]
        if existing:
            logger.info(
                "Resuming: %d already recorded, %d to run",
                len(existing),
                len(pending),
            )

        self._total = len(pending)
        if not pending:
            logger.info("Nothing to run.")
            return self._finish(list(existing.values()))

        logger.info(
            "Running %d instances | benchmark=%s harness=%s model=%s concurrency=%d",
            len(pending),
            self.benchmark,
            cfg.harness.name,
            cfg.model.name or "<unset>",
            cfg.concurrency,
        )

        async with AsyncSandboxClient(
            base_url=cfg.sandbox.base_url,
            api_key=cfg.sandbox.api_key,
            prefix=cfg.sandbox.prefix,
        ) as client:
            # Commands go through the job client rather than the SDK's exec,
            # which re-sends a command whose connection dropped — a second
            # agent CLI on the first one's tree. Built from the SDK client so
            # both resolve the same orchestrator and key.
            self._jobs = JobClient(client.base_url, api_key=client.api_key)
            try:
                results = await asyncio.gather(
                    *(self._run_guarded(client, inst) for inst in pending),
                    return_exceptions=True,
                )
            finally:
                await self._jobs.close()
                self._jobs = None

        records = list(existing.values())
        for instance, result in zip(pending, results, strict=True):
            if isinstance(result, BaseException):
                # Should be unreachable — _run_guarded catches everything — but
                # a lost instance must still appear in the results file.
                logger.error(
                    "[%s] unhandled runner error: %s", instance.instance_id, result
                )
                record = self._infra_failure_record(instance, result)
                await self.writer.append(record)
                records.append(record)
            else:
                records.append(result)

        return self._finish(records)

    def _finish(self, records: list[InstanceRecord]) -> list[InstanceRecord]:
        cfg = self.config
        elapsed = time.monotonic() - self._started_at
        summary = summarize(
            records, cfg, wall_clock_s=elapsed, benchmark=self.benchmark
        )
        write_summary(summary, cfg.run_dir / "summary.json")
        write_predictions(records, cfg.run_dir / "preds.json", cfg.model.name)
        write_report(format_report(summary, records), cfg.run_dir / "report.txt")
        logger.info(
            "Done: %d/%d resolved (%.1f%%) in %s",
            summary.resolved,
            summary.total,
            summary.resolve_rate * 100,
            format_duration(elapsed),
        )
        return records

    # ------------------------------------------------------------------

    async def _run_guarded(
        self, client: AsyncSandboxClient, instance: TaskInstance
    ) -> InstanceRecord:
        async with self._semaphore:
            try:
                record = await self._run_instance(client, instance)
            except Exception as exc:  # noqa: BLE001 - never lose an instance
                logger.exception("[%s] fatal error", instance.instance_id)
                record = self._infra_failure_record(instance, exc)
            await self.writer.append(record)
            await self._report_progress(record)
            return record

    async def _report_progress(self, record: InstanceRecord) -> None:
        async with self._progress_lock:
            self._done += 1
            self._resolved += int(record.resolved)
            logger.info(
                "[%d/%d] %s resolved=%s status=%s (%.0fs) | running rate %.1f%%",
                self._done,
                self._total,
                record.instance_id,
                record.resolved,
                record.exit_status,
                record.timings.total_s,
                100.0 * self._resolved / max(self._done, 1),
            )

    def _infra_failure_record(
        self, instance: TaskInstance, exc: BaseException
    ) -> InstanceRecord:
        return InstanceRecord(
            instance_id=instance.instance_id,
            harness=self.config.harness.name,
            model=self.config.model.name,
            run_name=self.config.run_name,
            exit_status=EXIT_INFRA_ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )

    # ------------------------------------------------------------------

    async def _run_instance(
        self, client: AsyncSandboxClient, instance: TaskInstance
    ) -> InstanceRecord:
        cfg = self.config
        instance_id = instance.instance_id
        artifacts_dir = cfg.instances_dir / instance_id if cfg.save_artifacts else None

        timings = Timings()
        started = time.monotonic()

        rollout = RolloutResult()
        sandbox_id = ""
        attempts = 1
        rollout_attempts = 1

        # A rollout that loses its pod is retried from scratch: the failure is
        # the cluster's, and the instance would otherwise drop out of the
        # benchmark without ever having been attempted. An agent that fails on a
        # healthy pod is a *result* and is never retried here.
        #
        # A deadline hit gets its own, smaller budget rather than none. It is
        # usually a property of the instance and a second pod rarely changes
        # it — but "the agent used its whole budget" and "the pod died in the
        # last seconds of it" look identical from here, and an instance that
        # would have scored is not worth losing to that ambiguity. Anything
        # this map does not name is a result and is kept as it is.
        retry_budget = {
            EXIT_INFRA_ERROR: max(cfg.rollout_retries, 0),
            EXIT_TIMEOUT: max(cfg.timeout_retries, 0),
        }
        for rollout_attempts in range(1, max(retry_budget.values()) + 2):
            try:
                create_started = time.monotonic()
                sandbox_instance, attempts = await self._create_sandbox(
                    client, instance, phase="rollout"
                )
                timings.create_s = round(time.monotonic() - create_started, 2)
            except Exception as exc:  # noqa: BLE001 - pod creation is the flaky part
                # _create_sandbox already spent its own cheap retries; a whole
                # fresh attempt is the next thing left to try.
                attempts = cfg.max_retries + 1
                rollout = RolloutResult(
                    exit_status=EXIT_INFRA_ERROR,
                    error=f"sandbox creation failed: {type(exc).__name__}: {exc}",
                )
            else:
                sandbox_id = sandbox_instance.sandbox_id
                try:
                    rollout = await self._drive_rollout(
                        sandbox_instance,
                        instance,
                        artifacts_dir=artifacts_dir,
                        timings=timings,
                    )
                except Exception as exc:  # noqa: BLE001 - classified, not swallowed
                    # The catch-all for everything a harness let through. Only
                    # a *sandbox* failure earns a new pod; anything else is the
                    # agent's and belongs in the record as it is.
                    if not is_sandbox_failure(exc):
                        raise
                    rollout = RolloutResult(
                        exit_status=EXIT_INFRA_ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                finally:
                    # The agent's pod is released *before* grading. Whatever the
                    # agent installed, cached, or monkey-patched dies with it, so
                    # the patch is scored in an environment it never touched.
                    await self._release(sandbox_instance, instance_id)

            budget = retry_budget.get(rollout.exit_status)
            if budget is None:
                break
            if rollout_attempts > budget:
                logger.error(
                    "[%s] giving up after %d rollout attempts (%s): %s",
                    instance_id,
                    rollout_attempts,
                    rollout.exit_status,
                    rollout.error,
                )
                # Budgets differ per status, so the loop bound alone no longer
                # ends the retries — the one that ran out has to say so.
                break
            logger.warning(
                "[%s] rollout %d/%d ended in %s (%s); retrying on a new pod",
                instance_id,
                rollout_attempts,
                budget + 1,
                rollout.exit_status,
                rollout.error,
            )
            await asyncio.sleep(min(2**rollout_attempts, 30) * (0.5 + random.random()))

        grade = GradeResult()
        eval_sandbox_id = ""
        if cfg.grading.enabled:
            # The patch is already in hand here, so a lost eval pod costs only a
            # pod — but leaving it unretried would waste the rollout that
            # produced the patch.
            for grade_attempt in range(1, cfg.rollout_retries + 2):
                grade, eval_sandbox_id = await self._grade_in_fresh_sandbox(
                    client,
                    instance,
                    rollout.patch,
                    artifacts_dir=artifacts_dir,
                    timings=timings,
                )
                if grade.resolved_status != "EVAL_INFRA_ERROR":
                    break
                if grade_attempt <= cfg.rollout_retries:
                    logger.warning(
                        "[%s] grading %d/%d lost its sandbox; retrying on a new pod",
                        instance_id,
                        grade_attempt,
                        cfg.rollout_retries + 1,
                    )
                    await asyncio.sleep(
                        min(2**grade_attempt, 30) * (0.5 + random.random())
                    )

        timings.total_s = round(time.monotonic() - started, 2)

        record = InstanceRecord(
            instance_id=instance_id,
            harness=cfg.harness.name,
            model=cfg.model.name,
            run_name=cfg.run_name,
            resolved=grade.resolved,
            reward=grade.reward,
            exit_status=rollout.exit_status,
            resolved_status=grade.resolved_status,
            patch=rollout.patch,
            sandbox_id=sandbox_id,
            eval_sandbox_id=eval_sandbox_id,
            attempts=attempts,
            rollout_attempts=rollout_attempts,
            n_messages=len(rollout.messages),
            error=rollout.error or grade.error,
            timings=timings,
            metrics=rollout.metrics,
            tests_status=grade.tests_status,
        )
        if artifacts_dir is not None:
            _write_artifact(
                artifacts_dir,
                "record.json",
                json.dumps(record.model_dump(mode="json"), indent=2),
            )
        return record

    async def _create_sandbox(
        self, client: AsyncSandboxClient, instance: TaskInstance, *, phase: str
    ) -> tuple[Any, int]:
        """Create one pod for ``instance``, retrying transient cluster refusals.

        Returns the instance and the number of attempts it took. Raises the last
        error once ``max_retries`` is exhausted.
        """
        cfg = self.config
        last_error: BaseException = RuntimeError("sandbox creation failed")

        for attempt in range(1, cfg.max_retries + 2):
            try:
                sandbox_instance = await client.create_sandbox(
                    image=instance.image,
                    block_network=cfg.sandbox.block_network,
                    cpu=cfg.sandbox.cpu,
                    memory=cfg.sandbox.memory,
                    timeout=cfg.sandbox.create_timeout,
                )
                return sandbox_instance, attempt
            except Exception as exc:  # noqa: BLE001 - pod creation is the flaky part
                last_error = exc
                logger.warning(
                    "[%s] %s sandbox creation failed (attempt %d/%d): %s",
                    instance.instance_id,
                    phase,
                    attempt,
                    cfg.max_retries + 1,
                    exc,
                )
                if attempt <= cfg.max_retries:
                    # Jittered backoff: a cluster that just refused one pod is
                    # usually about to refuse a synchronized retry storm too.
                    await asyncio.sleep(min(2**attempt, 30) * (0.5 + random.random()))

        raise last_error

    async def _drive_rollout(
        self,
        sandbox_instance,
        instance: TaskInstance,
        *,
        artifacts_dir: Path | None,
        timings: Timings,
    ) -> RolloutResult:
        cfg = self.config
        sandbox = EvalSandbox(
            sandbox_instance,
            workdir=instance.workdir,
            loop=asyncio.get_running_loop(),
            default_timeout=cfg.sandbox.command_timeout,
            jobs=self._jobs,
        )

        setup_started = time.monotonic()
        await sandbox.prepare_repo(instance.base_commit)
        timings.setup_s = round(time.monotonic() - setup_started, 2)

        ctx = RolloutContext(
            sandbox=sandbox,
            instance=instance,
            config=cfg,
            artifacts_dir=artifacts_dir,
            logger=logger,
        )

        rollout_started = time.monotonic()
        rollout: RolloutResult = await self.harness.rollout(ctx)
        timings.rollout_s = round(time.monotonic() - rollout_started, 2)

        # Trajectory persistence lives HERE, not in the harness, so that every
        # harness — including ones added later — produces one without having to
        # remember to. Trajectories are the primary artifact for distillation
        # and RL; a harness that silently stopped saving them would not be
        # noticed until the data was needed.
        self._save_trajectory(artifacts_dir, instance, rollout)

        if artifacts_dir is not None and rollout.patch:
            _write_artifact(artifacts_dir, "patch.diff", rollout.patch)

        return rollout

    async def _grade_in_fresh_sandbox(
        self,
        client: AsyncSandboxClient,
        instance: TaskInstance,
        patch: str,
        *,
        artifacts_dir: Path | None,
        timings: Timings,
    ) -> tuple[GradeResult, str]:
        """Score ``patch`` in a pod the agent never had access to.

        This mirrors upstream SWE-bench, which builds a container per prediction
        from the instance image and does nothing in it but apply the patch and
        run the tests.
        """
        cfg = self.config
        if not patch.strip() and not cfg.grading.grade_empty_patch:
            # Cheaper than a pod, and the verdict is identical.
            return GradeResult.unresolved("empty patch", status="EMPTY_PATCH"), ""

        create_started = time.monotonic()
        try:
            sandbox_instance, _ = await self._create_sandbox(
                client, instance, phase="eval"
            )
        except Exception as exc:  # noqa: BLE001 - a lost pod must not lose the run
            logger.error(
                "[%s] eval sandbox creation failed: %s", instance.instance_id, exc
            )
            return (
                GradeResult.unresolved(
                    f"eval sandbox creation failed: {exc}", status="EVAL_INFRA_ERROR"
                ),
                "",
            )
        timings.grade_create_s = round(time.monotonic() - create_started, 2)

        try:
            sandbox = EvalSandbox(
                sandbox_instance,
                workdir=instance.workdir,
                loop=asyncio.get_running_loop(),
                default_timeout=cfg.sandbox.command_timeout,
                jobs=self._jobs,
            )
            grade_started = time.monotonic()
            grade = await self._grade(sandbox, instance, patch, artifacts_dir)
            timings.grade_s = round(time.monotonic() - grade_started, 2)
            return grade, sandbox_instance.sandbox_id
        except Exception as exc:  # noqa: BLE001 - classified, not swallowed
            # A pod reaped or 5xx-ing mid-grade is an infrastructure failure,
            # not a zero: letting it escape would discard the rollout record
            # along with it.
            if not is_sandbox_failure(exc):
                raise
            logger.error("[%s] eval sandbox lost: %s", instance.instance_id, exc)
            return (
                GradeResult.unresolved(
                    f"{type(exc).__name__}: {exc}", status="EVAL_INFRA_ERROR"
                ),
                sandbox_instance.sandbox_id,
            )
        finally:
            await self._release(sandbox_instance, instance.instance_id)

    async def _grade(
        self,
        sandbox: EvalSandbox,
        instance: TaskInstance,
        patch: str,
        artifacts_dir: Path | None,
    ) -> GradeResult:
        """Score ``patch`` with the grader the run's benchmark calls for."""
        cfg = self.config
        save_log = (
            (lambda name, content: _write_artifact(artifacts_dir, name, content))
            if artifacts_dir is not None
            else None
        )
        if self.benchmark == BENCHMARK_SWEBENCH_PRO:
            assert self._pro_scripts is not None  # set alongside self.benchmark
            return await grade_swebench_pro_patch(
                sandbox,
                instance,
                patch,
                scripts=self._pro_scripts,
                eval_timeout=cfg.grading.eval_timeout,
                apply_timeout=cfg.grading.apply_timeout,
                allow_empty=cfg.grading.grade_empty_patch,
                save_log=save_log,
            )
        return await grade_swebench_patch(
            sandbox,
            instance,
            patch,
            eval_timeout=cfg.grading.eval_timeout,
            apply_timeout=cfg.grading.apply_timeout,
            allow_empty=cfg.grading.grade_empty_patch,
            save_log=save_log,
        )

    async def _release(self, sandbox_instance, instance_id: str) -> None:
        if self.config.keep_sandbox:
            logger.info(
                "[%s] keeping sandbox %s alive (keep_sandbox=true)",
                instance_id,
                sandbox_instance.sandbox_id,
            )
            return
        await self._safe_delete(sandbox_instance)

    @staticmethod
    async def _safe_delete(sandbox_instance) -> None:
        try:
            await sandbox_instance.delete()
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            logger.debug("sandbox delete failed: %s", exc)

    def _save_trajectory(
        self,
        artifacts_dir: Path | None,
        instance: TaskInstance,
        rollout: RolloutResult,
    ) -> None:
        """Persist the trajectory for any harness, in a uniform envelope.

        Writes ``trajectory.json``: normalized messages plus run metadata. A
        harness that genuinely captured nothing still gets the file, recording
        that fact, so an empty trajectory is distinguishable from a run that
        never wrote one.
        """
        if artifacts_dir is None:
            return

        cfg = self.config
        envelope = {
            "instance_id": instance.instance_id,
            "harness": cfg.harness.name,
            "model": cfg.model.name,
            "run_name": cfg.run_name,
            "exit_status": rollout.exit_status,
            "n_messages": len(rollout.messages),
            "messages": rollout.messages,
            "submission": rollout.submission,
            "metrics": rollout.metrics,
            "info": rollout.trajectory_extra,
            "error": rollout.error,
        }
        if not rollout.messages:
            logger.warning(
                "[%s] harness %r captured no trajectory messages",
                instance.instance_id,
                cfg.harness.name,
            )

        _write_artifact(
            artifacts_dir,
            "trajectory.json",
            json.dumps(envelope, indent=2, default=str),
        )


def _write_artifact(directory: Path | None, name: str, content: str) -> None:
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(content, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk-full etc.
        logger.warning("could not write artifact %s: %s", name, exc)


async def run_eval(
    config: RunConfig, instances: list[TaskInstance] | None = None
) -> list[InstanceRecord]:
    """Convenience entry point used by the CLI and by external callers."""
    return await EvalRunner(config).run(instances)


__all__ = ["EvalRunner", "ResultWriter", "load_existing_records", "run_eval"]
