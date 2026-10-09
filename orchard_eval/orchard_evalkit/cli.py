"""``orchard-eval`` command-line interface.

Three subcommands:

``run``
    Evaluate a harness on a benchmark.
``report``
    Re-aggregate an existing run directory (useful after a partial run).
``list-harnesses``
    Show which harnesses this installation can drive.

Configuration is layered — a YAML file supplies the shape of the run, repeated
``-c``/``--config`` files merge on top of each other, and trailing ``key=value``
arguments override anything. Frequently-used knobs also have real flags.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from orchard_evalkit.config import (
    ROUTING_MODES,
    RunConfig,
    expand_ports,
    load_config,
    parse_overrides,
)
from orchard_evalkit.harnesses import harness_descriptions
from orchard_evalkit.report import (
    format_report,
    format_summary,
    summarize,
    write_report,
)
from orchard_evalkit.runner import load_existing_records, run_eval


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # These are chatty at DEBUG and drown out the run's own progress lines.
    for noisy in (
        "urllib3",
        "httpx",
        "httpcore",
        "openai",
        "LiteLLM",
        "asyncio",
        "filelock",
        "fsspec",
        "datasets",
        "huggingface_hub",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


#: Flags that map onto dotted config paths, so ``--limit 5`` and
#: ``dataset.limit=5`` are exactly equivalent.
_FLAG_TO_CONFIG_PATH = {
    "harness": "harness.name",
    "wire_api": "harness.params.wire_api",
    "model": "model.name",
    "base_url": "model.base_url",
    "base_url_replicas": "model.base_url_replicas",
    "routing": "model.routing",
    "api_key_env": "model.api_key_env",
    "dataset": "dataset.name",
    "benchmark": "dataset.benchmark",
    "split": "dataset.split",
    "limit": "dataset.limit",
    "filter": "dataset.filter",
    "slice": "dataset.slice",
    "concurrency": "concurrency",
    "output_dir": "output_dir",
    "run_name": "run_name",
    "run_id": "run_id",
    "sandbox_url": "sandbox.base_url",
    "image_prefix": "sandbox.image_prefix",
    "log_level": "log_level",
}


def _build_run_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "run",
        help="Evaluate a harness on a benchmark",
        description="Evaluate an agent harness on SWE-bench using Orchard Env sandboxes.",
    )
    parser.add_argument(
        "-c",
        "--config",
        action="append",
        default=[],
        help="YAML config file. Repeatable; later files merge over earlier ones.",
    )
    parser.add_argument(
        "--harness", help="Harness name, e.g. codex, pi, mini-swe-agent"
    )
    parser.add_argument(
        "--wire-api",
        choices=["responses", "chat"],
        help=(
            "Protocol the agent CLI speaks to the model endpoint. Use 'chat' for "
            "a server without a working /v1/responses route."
        ),
    )
    parser.add_argument("--model", help="Model identifier passed to the harness")
    parser.add_argument("--base-url", help="OpenAI-compatible endpoint for the model")
    parser.add_argument(
        "--base-url-replicas",
        type=int,
        help=(
            "Treat --base-url as the first of N endpoints on consecutive ports. "
            "Each rollout is pinned to one of them, so its agent loop reuses that "
            "server's KV cache."
        ),
    )
    parser.add_argument(
        "--routing",
        choices=list(ROUTING_MODES),
        help=(
            "How a rollout picks its endpoint: 'sticky' hashes the instance id "
            "(reproducible, survives retries), 'random' draws per rollout, "
            "'session' names the instance in the URL path and lets "
            "scripts/session_router.py place it on the least-loaded engine."
        ),
    )
    parser.add_argument(
        "--api-key-env",
        help="Environment variable holding the model API key (never the key itself)",
    )
    parser.add_argument(
        "--dataset",
        help=(
            "Dataset name: swe-bench-verified, swe-bench-lite, swe-bench-full, "
            "swe-bench-pro, a HuggingFace id, or a .jsonl path"
        ),
    )
    parser.add_argument(
        "--benchmark",
        choices=["auto", "swe-bench", "swe-bench-pro"],
        help=(
            "Which benchmark's image naming, repo path and grading apply. "
            "Inferred from --dataset when left out."
        ),
    )
    parser.add_argument("--split", help="Dataset split (default: test)")
    parser.add_argument("--limit", type=int, help="Run at most N instances")
    parser.add_argument("--filter", help="Regex matched against instance_id")
    parser.add_argument("--slice", help="Slice spec, e.g. 0:25")
    parser.add_argument(
        "--instance-id",
        action="append",
        default=[],
        help="Run only this instance. Repeatable, or comma-separated.",
    )
    parser.add_argument("--concurrency", type=int, help="Sandboxes to run in parallel")
    parser.add_argument("--output-dir", help="Where run directories are written")
    parser.add_argument("--run-name", help="Name of this run's output directory")
    parser.add_argument(
        "--run-id",
        help=(
            "Timestamped subdirectory under the run directory. Defaults to now, "
            "or to the newest existing attempt when resuming."
        ),
    )
    parser.add_argument("--sandbox-url", help="Orchestrator URL (or SANDBOX_BASE_URL)")
    parser.add_argument("--image-prefix", help="Registry holding SWE-bench images")
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Re-run instances already present in results.jsonl",
    )
    parser.add_argument(
        "--no-grading",
        action="store_true",
        help="Collect patches and trajectories without running the benchmark tests",
    )
    parser.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="Leave sandboxes running after each instance (debugging)",
    )
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument(
        "overrides",
        nargs="*",
        help="Dotted config overrides, e.g. harness.params.step_timeout=120",
    )


def _overrides_from_args(args: argparse.Namespace) -> list[str]:
    """Translate real flags into the same dotted overrides users can type."""
    overrides: list[str] = []
    for flag, path in _FLAG_TO_CONFIG_PATH.items():
        value = getattr(args, flag, None)
        if value is not None:
            overrides.append(f"{path}={value}")
    if args.instance_id:
        ids = [i for spec in args.instance_id for i in spec.split(",") if i]
        overrides.append(f"dataset.instance_ids={ids}")
    if args.no_resume:
        overrides.append("resume=false")
    if args.no_grading:
        overrides.append("grading.enabled=false")
    if args.keep_sandbox:
        overrides.append("keep_sandbox=true")
    # User-supplied overrides go last so they win over every flag above.
    overrides.extend(args.overrides)
    return overrides


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config, _overrides_from_args(args))
    _configure_logging(config.log_level)

    logging.getLogger(__name__).info("Run directory: %s", config.run_dir)
    started = time.monotonic()
    records = asyncio.run(run_eval(config))
    elapsed = time.monotonic() - started
    summary = summarize(records, config, wall_clock_s=elapsed)
    print(format_summary(summary))
    report_path = config.run_dir / "report.txt"
    write_report(format_report(summary, records), report_path)
    print(f"  report saved to {report_path}\n")
    return 0

def _cmd_report(args: argparse.Namespace) -> int:
    _configure_logging(args.log_level or "INFO")
    overrides = parse_overrides(args.overrides)
    config = RunConfig(**overrides) if overrides else None

    results_path = args.results
    if results_path is None:
        if config is None:
            raise SystemExit(
                "Pass --results PATH or config overrides identifying a run"
            )
        results_path = str(config.results_path)

    records = list(load_existing_records(Path(results_path)).values())
    if not records:
        raise SystemExit(f"No records found in {results_path}")
    summary = summarize(records, config)
    print(format_summary(summary))
    report_path = Path(results_path).parent / "report.txt"
    write_report(format_report(summary, records), report_path)
    print(f"  report saved to {report_path}\n")
    return 0


def _cmd_list_harnesses(_: argparse.Namespace) -> int:
    descriptions = harness_descriptions()
    width = max(len(name) for name in descriptions)
    print("\nAvailable harnesses:\n")
    for name, description in descriptions.items():
        print(f"  {name:<{width}}  {description}")
    print()
    return 0


def _cmd_harbor(args: argparse.Namespace) -> int:
    """Run a Harbor dataset and report on it in this suite's terms."""
    from orchard_evalkit import harbor_bridge

    _configure_logging(args.log_level)

    spec = harbor_bridge.HarborRunSpec(
        dataset=args.dataset,
        path=args.path,
        agent=args.agent,
        model=args.model,
        jobs_dir=Path(args.jobs_dir),
        job_name=args.job_name,
        n_concurrent=args.concurrency,
        n_attempts=args.attempts,
        infra_retries=args.infra_retries,
        include_tasks=args.task + _read_task_files(args.task_file),
        exclude_tasks=args.exclude_task
        + _read_task_files(args.exclude_task_file, "--exclude-task-file"),
        n_tasks=args.limit,
        extra_args=args.harbor_args,
        base_urls=_harbor_endpoints(args),
        routing=args.routing,
        wire_api=args.wire_api,
    )

    # Snapshot the job directories so the summary covers this run only, not
    # every run that ever wrote into --jobs-dir.
    existing_jobs = harbor_bridge.job_dirs(spec.jobs_dir)
    exit_code = harbor_bridge.run(spec, env=spec.environment())
    job_dir = harbor_bridge.resolve_job_dir(spec.jobs_dir, existing_jobs)

    # Summarize whatever finished, even when harbor itself exited non-zero: a
    # run that died at task 60 of 66 still tells you what the first 59 did.
    outcomes = harbor_bridge.collect_outcomes(job_dir)
    if not outcomes:
        # Harbor reports its own exception table above, but the cause is always
        # in the trial log, so name the file rather than leaving a dead end.
        print(f"\n  No trial results found under {job_dir}")
        print("  Harbor writes the cause to the trial log:")
        print(f"    tail -50 {job_dir}/*/trial.log\n")
        return exit_code or 1

    label = "oracle solve rate" if args.agent == "oracle" else f"{args.agent} solve rate"
    summary = harbor_bridge.summarize(outcomes)
    print(harbor_bridge.format_summary(summary, label=label))

    try:
        saved = harbor_bridge.write_summary(job_dir, summary, label=label)
        print(f"  summary saved to {saved}\n")
    except OSError as exc:
        logging.getLogger(__name__).warning("could not write the summary: %s", exc)

    if args.threshold is not None and summary["solve_rate"] < args.threshold:
        print(
            f"  FAIL: {summary['solve_rate']:.2%} is below the "
            f"{args.threshold:.2%} threshold.\n"
        )
        return 1
    if args.max_solve_rate is not None and summary["solve_rate"] > args.max_solve_rate:
        # The floor is a claim about the grader, not about a model: tasks that
        # score without a solution are tasks whose verifier passes on the
        # untouched repository, and every number measured on them is inflated.
        print(
            f"  FAIL: {summary['solve_rate']:.2%} is above the "
            f"{args.max_solve_rate:.2%} floor — those tasks pass without a solution.\n"
        )
        return 1
    return exit_code


def _read_task_files(paths: list[str], flag: str = "--task-file") -> list[str]:
    """Task names read from files, so a long list is not a long command line.

    A subset worth re-running is usually derived rather than typed — the 88
    musl instances in ``configs/swebench-pro-musl.txt``, or whatever
    ``scripts/harbor_failed_tasks.py`` printed for a given cause — and repeating
    ``--task`` 88 times is not a usable way to hand that back.

    ``#`` comments and blank lines are skipped so a list can explain itself.
    """
    names: list[str] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"could not read {flag} {path}: {exc}") from exc
        for line in text.splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                names.append(entry)
    return names


def _harbor_endpoints(args: argparse.Namespace) -> list[str]:
    """Every endpoint a Harbor job may use, in a stable order."""
    base_url = args.base_url or os.environ.get("MODEL_BASE_URL", "").strip()
    if not base_url:
        return []
    replicas = args.base_url_replicas or int(
        os.environ.get("MODEL_BASE_URL_REPLICAS", "1")
    )
    if replicas < 1:
        raise SystemExit("--base-url-replicas must be >= 1")
    return expand_ports(base_url, replicas) if replicas > 1 else [base_url]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchard-eval",
        description="Benchmark agent harnesses on Orchard Env sandboxes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _build_run_parser(subparsers)

    report = subparsers.add_parser(
        "report", help="Re-aggregate an existing run's results.jsonl"
    )
    report.add_argument("--results", help="Path to results.jsonl")
    report.add_argument("--log-level", default="INFO")
    report.add_argument("overrides", nargs="*", help="Dotted config overrides")

    subparsers.add_parser("list-harnesses", help="Show the harnesses available here")

    _build_harbor_parser(subparsers)

    return parser


def _build_harbor_parser(subparsers: argparse._SubParsersAction) -> None:
    from orchard_evalkit.harbor_bridge import DEFAULT_INFRA_RETRIES

    parser = subparsers.add_parser(
        "harbor",
        help="Run a Harbor dataset (terminal-bench, ...) on Orchard sandboxes",
        description=(
            "Drives `harbor run` with the Orchard environment provider. Harbor "
            "owns the benchmark; this repository owns the environment it runs in."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "-d", "--dataset", help="Hub dataset, e.g. terminal-bench/terminal-bench@4.0.0"
    )
    source.add_argument("-p", "--path", help="Local dataset or task directory")
    parser.add_argument(
        "--agent",
        default="oracle",
        help="Harbor agent. 'oracle' replays each task's own solution (default)",
    )
    parser.add_argument("--model", help="Model id, required by every agent but oracle")
    parser.add_argument(
        "--base-url",
        help=(
            "OpenAI-compatible endpoint the agent uses from inside the pod. "
            "Defaults to $MODEL_BASE_URL."
        ),
    )
    parser.add_argument(
        "--base-url-replicas",
        type=int,
        help=(
            "Treat --base-url as the first of N endpoints on consecutive ports. "
            "Each trial is pinned to one of them, so its agent loop reuses that "
            "server's KV cache. Defaults to $MODEL_BASE_URL_REPLICAS."
        ),
    )
    parser.add_argument(
        "--routing",
        choices=list(ROUTING_MODES),
        # The provider reads MODEL_ROUTING from its own environment, but the
        # bridge passes this value down explicitly and so wins over it. A plain
        # default="sticky" therefore silently overrode an exported
        # MODEL_ROUTING, which the wrapper scripts document as the way to set
        # it. Defaulting *to* the variable keeps the flag winning when given
        # and makes the documented export work when it is not.
        default=os.environ.get("MODEL_ROUTING") or "sticky",
        help=(
            "How a trial picks its endpoint: 'sticky' hashes the task name "
            "(reproducible, survives retries), 'random' draws per trial, "
            "'session' names the task in the URL path and lets "
            "scripts/session_router.py place it on the least-loaded engine. "
            "Defaults to $MODEL_ROUTING, then 'sticky'."
        ),
    )
    parser.add_argument(
        "--wire-api",
        choices=["responses", "chat"],
        default="responses",
        help=(
            "Protocol the staged codex config declares. Use 'chat' for a server "
            "without a working /v1/responses route."
        ),
    )
    parser.add_argument("--jobs-dir", default="./results/harbor")
    parser.add_argument("--job-name", default="")
    parser.add_argument("-n", "--concurrency", type=int, default=8)
    parser.add_argument(
        "-k", "--attempts", type=int, default=1, help="Trials per task"
    )
    parser.add_argument(
        "--infra-retries",
        type=int,
        default=DEFAULT_INFRA_RETRIES,
        help=(
            "Rerun a trial whose sandbox or orchestrator failed under it, as a "
            "new trial in a new pod, up to this many times. Agent timeouts and "
            "agent errors are results and are never rerun. 0 disables. "
            "Default: %(default)s"
        ),
    )
    parser.add_argument("--task", action="append", default=[], help="Only this task. Repeatable")
    parser.add_argument(
        "--exclude-task", action="append", default=[], help="Skip this task. Repeatable"
    )
    parser.add_argument(
        "--task-file",
        action="append",
        default=[],
        help=(
            "Read task names to include from a file, one per line ('#' comments "
            "skipped). Repeatable, and combines with --task. "
            "configs/swebench-pro-musl.txt is one such list."
        ),
    )
    parser.add_argument(
        "--exclude-task-file",
        action="append",
        default=[],
        help="Read task names to skip from a file. Repeatable",
    )
    parser.add_argument("--limit", type=int, help="Run at most this many tasks")
    parser.add_argument(
        "--threshold",
        type=float,
        help=(
            "Exit non-zero below this solve rate. Use with --agent oracle to gate "
            "CI: the oracle score caps every model score measured afterwards."
        ),
    )
    parser.add_argument(
        "--max-solve-rate",
        type=float,
        help=(
            "Exit non-zero ABOVE this solve rate. Use with an agent that does "
            "nothing to gate the floor: anything it solves is a task whose "
            "verifier passes on the untouched repository."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "harbor_args",
        nargs="*",
        help="Extra arguments forwarded verbatim to `harbor run`",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "run": _cmd_run,
        "report": _cmd_report,
        "list-harnesses": _cmd_list_harnesses,
        "harbor": _cmd_harbor,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
