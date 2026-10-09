#!/usr/bin/env python3
"""Collect every benchmark's score for one model into a single table.

``run_all_evals.sh`` writes up to 18 runs under one prefix, in two different
shapes: ``orchard-eval run`` leaves a ``summary.json`` per run, and Harbor
leaves a ``result.json`` per *trial*. This reads both and prints two matrices —
the scores, then how long each job took.

SWE-bench Pro can be measured two ways, as *SWE-bench Pro* and *SWE-bench Pro
(Harbor)*: the same 731 instances graded by two independent implementations;
``compare_swebench_pro.py`` says which instances they disagreed on. Only the
Harbor column is shown by default.

SWE-bench Pro V2 is a different benchmark and gets columns of its own: *SWE-bench
Pro V2 (re-grade)*, the patch replayed in a fresh sandbox and the number to
publish, and *SWE-bench Pro V2 (Harbor)*, the agent's own container — hidden by
default, since the re-grade supersedes it. Each is
followed by a *HARD-51* column: the same job's trials restricted to upstream's
``v2/hard51_ids.txt``, read from ``$PRO_V2_DIR`` (default
``third_party/SWE-bench_Pro-os``) or ``--hard51-file``.

``--all-benchmarks`` brings back the two hidden columns, *SWE-bench Pro* and
*SWE-bench Pro V2 (Harbor)*; without it their jobs are listed as hidden and
never read.

    python scripts/results_table.py results/qwen3.5-35b-a3b-iter29
    python scripts/results_table.py results/<tag> --counts
    python scripts/results_table.py results/<tag> --all-benchmarks
    python scripts/results_table.py results/<tag> --no-times
    python scripts/results_table.py results/<tag> --format markdown
    python scripts/results_table.py results/<tag> --format csv > scores.csv

The scores come from one small file per run. A Harbor job is timed from its
trials' own ``started_at`` / ``finished_at``, read from those same files; only a
SWE-bench run still in progress, or a Harbor job whose trials carry no
timestamps, is dated by stat-ing every artifact under it. ``--no-times`` skips
the wall-clock table altogether.

With no argument it reads ``results/$MODEL_TAG``. Pass several directories to
compare models; each becomes its own pair of tables.

Safe to run *during* a long run: an ``orchard-eval run`` appends to
``results.jsonl`` as each instance finishes and Harbor writes a trial's
``result.json`` as each trial finishes, so a cell can be scored before its job
ends. Those cells are marked ``*`` — their denominator is what has completed so
far, not the size of the benchmark.

    watch -n 120 python scripts/results_table.py results/<tag> --counts

A benchmark whose verifier reports a partial score of its own — DeepSWE does,
as the ``Partial`` column of Harbor's summary table — gets a second
``(partial)`` column beside its pass rate, reading ``71.90% (95.20%, 23.70%)``:
the partial score, then the share of P2P and of F2P tests left green. See
``partial_credit`` and ``test_rates`` for what those numbers are and are not.

Two more tables say what each score cost: the mean number of agent turns per
trial — LLM responses, one per call — and the mean input / output tokens the
agent spent across those calls. Each cell is the mean over every trial that
recorded it, then in parentheses over the solved trials alone. A re-grade
column inherits both from the agent run whose patch it replayed. Turns are read
from each trial's trajectory, which costs one more file per trial;
``--no-efficiency`` skips both tables.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchard_evalkit.harbor_bridge import (  # noqa: E402
    TrialOutcome,
    agent_tokens,
    collect_outcomes,
    newest_attempt,
    summarize,
)

# Longest first when matching a prefix; this order is the row order.
HARNESSES = [
    "mini-swe-agent",
    "pi",
    "codex",
    "claude-code",
    "opencode",
    "gold",
    "noop",
]

#: Directory-name fragment -> column heading, in the order they are matched.
#:
#: Matched as a substring, longest-qualifying-first *by list order* rather than
#: by length: ``swebench-pro-harbor`` has to precede ``swebench-pro`` or the
#: Harbor cross-check job lands in the column of the run it exists to be
#: compared against, and one job's score overwrites the other's cell. The V2
#: jobs, a different benchmark entirely, need the same care, and the V2
#: re-grade has to precede V2 itself for the same reason, and a HARD-51-only run
#: (run_all_evals.sh with PRO_V2_HARD51=1) has to precede both, or its 51 trials
#: would take over the full set's cell.
BENCHMARKS = [
    ("swebench-verified", "SWE-bench Verified"),
    ("swebench-multilingual", "SWE-bench Multilingual"),
    ("swebench-pro-harbor", "SWE-bench Pro (Harbor)"),
    ("swebench-pro-v2-hard51-regrade", "SWE-bench Pro V2 HARD-51 (re-grade)"),
    ("swebench-pro-v2-hard51", "SWE-bench Pro V2 HARD-51 (Harbor)"),
    ("swebench-pro-v2-regrade", "SWE-bench Pro V2 (re-grade)"),
    ("swebench-pro-v2", "SWE-bench Pro V2 (Harbor)"),
    ("swebench-pro", "SWE-bench Pro"),
    ("tb2.1", "Terminal-Bench 2.1"),
    ("deepswe1.1", "DeepSWE 1.1"),
]

#: The order columns appear in, which the matching order above cannot also be.
COLUMN_ORDER = [
    "SWE-bench Verified",
    "SWE-bench Multilingual",
    "SWE-bench Pro (Harbor)",
    "SWE-bench Pro",
    "SWE-bench Pro V2 (Harbor)",
    "SWE-bench Pro V2 (re-grade)",
    "SWE-bench Pro V2 HARD-51 (Harbor)",
    "SWE-bench Pro V2 HARD-51 (re-grade)",
    "Terminal-Bench 2.1",
    "DeepSWE 1.1",
]

#: Columns left out unless ``--all-benchmarks`` is passed. The non-Harbor V1 run
#: is superseded by *SWE-bench Pro (Harbor)*, and V2's direct score by its
#: re-grade, the number to publish.
HIDDEN_BY_DEFAULT = frozenset(
    {"SWE-bench Pro", "SWE-bench Pro V2 (Harbor)", "SWE-bench Pro V2 HARD-51 (Harbor)"}
)

EMPTY = "-"

#: Harbor jobs scored at once; each also reads its own trials in parallel.
JOB_WORKERS = 8

#: Trial trajectories read at once per job, for the turn counts.
TRAJECTORY_WORKERS = 16

#: The verifier's own partial-credit metric, as Harbor's summary table spells it.
PARTIAL_KEY = "partial"

#: Columns that get a HARD-51 sub-score beside them. V1's task names are
#: ``scale-ai/<id>`` over overlapping ids, so the subset is kept to V2's columns.
HARD_LABEL_PREFIX = "SWE-bench Pro V2"
HARD_SUBSET = "HARD-51"


def wants_hard_column(label: str) -> bool:
    """A full V2 column gets a HARD-51 sub-score; a HARD-51-only one already is it."""
    return label.startswith(HARD_LABEL_PREFIX) and HARD_SUBSET not in label


def default_hard_file() -> Path:
    pro_v2_dir = os.environ.get("PRO_V2_DIR") or str(
        Path(__file__).resolve().parent.parent / "third_party" / "SWE-bench_Pro-os"
    )
    return Path(pro_v2_dir) / "v2" / "hard51_ids.txt"


def read_hard_ids(path: Path) -> frozenset[str]:
    """Bare instance ids, one per line; ``#`` comments and blanks skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(
        entry
        for line in text.splitlines()
        if (entry := line.split("#", 1)[0].strip())
    )


def bare_task_id(task: str) -> str:
    """``swebench-pro/instance_…`` -> ``instance_…``, as hard51_ids.txt spells it."""
    return task.rsplit("/", 1)[-1]


class Effort(NamedTuple):
    """Per-trial means of what the agent spent; None where nothing recorded it."""

    turns: float | None = None
    input_tokens: float | None = None
    output_tokens: float | None = None


class Spent(NamedTuple):
    """One trial's turns and tokens, beside whether it was solved."""

    solved: bool
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class Score(NamedTuple):
    solved: int
    total: int
    #: Mean of the verifier's own partial score, where it reports one.
    partial: float | None = None
    #: Mean share of PASS_TO_PASS tests left green, where the tests are counted.
    p2p: float | None = None
    #: Mean share of FAIL_TO_PASS tests turned green, same caveat.
    f2p: float | None = None
    #: Scored from a job that has not finished; ``total`` is what completed.
    running: bool = False
    #: Wall clock for the whole job, not the sum of its concurrent instances.
    elapsed_s: float | None = None
    #: Solved / scored over the HARD-51 trials only, for SWE-bench Pro V2 jobs.
    hard_solved: int | None = None
    hard_total: int | None = None
    #: Mean turns and tokens over every trial, and over the solved ones alone.
    effort: Effort | None = None
    effort_solved: Effort | None = None


def split_run_name(name: str) -> tuple[str, str]:
    """``mini-swe-agent-swebench-pro`` -> ``(mini-swe-agent, swebench-pro)``."""
    base = re.sub(r"-smoke\d*$", "", name)
    for harness in sorted(HARNESSES, key=len, reverse=True):
        if base == harness:
            return harness, ""
        if base.startswith(harness + "-"):
            return harness, base[len(harness) + 1 :]
    return "", base


def benchmark_label(fragment: str) -> str:
    """Substring, not equality: a job directory may carry a suffix of its own."""
    # run_swebench_pro_v2.sh names a re-grade <agent>-regrade-swebench-pro-v2-<stamp>,
    # where run_all_evals.sh puts the -regrade after the benchmark.
    prefix = "regrade-swebench-pro-v2"
    if fragment.startswith(prefix):
        fragment = "swebench-pro-v2-regrade" + fragment[len(prefix) :]
    for key, label in BENCHMARKS:
        if key in fragment:
            return label
    return fragment


def trial_span_s(outcomes: list[TrialOutcome]) -> float | None:
    """Wall clock from the trials' own timestamps, first start to last finish.

    Read from the ``result.json`` files the scores already came from, so it
    costs nothing extra. For a job still running it ends at the last trial to
    finish rather than at now. None when Harbor recorded no timestamps, which
    sends the caller back to ``dir_span_s``.
    """
    starts = [o.started_at for o in outcomes if o.started_at is not None]
    ends = [o.finished_at for o in outcomes if o.finished_at is not None]
    if not starts or not ends:
        return None
    try:
        span = (max(ends) - min(starts)).total_seconds()
    except TypeError:
        return None  # naive and aware timestamps mixed; not comparable
    return span if span > 0 else None


def dir_span_s(path: Path) -> float | None:
    """Wall clock inferred from the artifacts, oldest write to newest.

    The fallback for a Harbor job whose trials recorded no timestamps, and the
    only timing for a SWE-bench run that has not written its ``summary.json``
    yet. Close enough to read a progress table by; the exact figure is
    ``wall_clock_s`` once the run finishes.

    This stats every artifact under *path*, which on NFS is slow; ``--no-times``
    skips it entirely.
    """
    oldest = newest = None
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                mtime = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            if oldest is None or mtime < oldest:
                oldest = mtime
            if newest is None or mtime > newest:
                newest = mtime
    if oldest is None or newest == oldest:
        return None
    return newest - oldest


def _partial_value(outcome: TrialOutcome) -> float | None:
    """One trial's partial score, preferring what its verifier itself reported."""
    reported = outcome.rewards.get(PARTIAL_KEY)
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return float(reported)
    counts = outcome.test_counts
    if counts is not None and counts[1]:
        return counts[0] / counts[1]
    return None


def partial_credit(outcomes: list[TrialOutcome]) -> float | None:
    """Mean of the verifier's own partial score, across every trial.

    Harbor's reward is all-or-nothing — every F2P test green *and* no P2P
    regression — so a 0 hides the difference between an agent that fixed 9 tests
    of 10 and one that did nothing. DeepSWE's verifier reports the softer number
    itself, as ``partial``: the column of that name in Harbor's per-job summary,
    which is *not* its ``F2P`` column and is generally much higher.

    How that score is composed is the task's business, not this script's. Where
    a verifier reports no ``partial``, the fraction of F2P tests green stands in
    for it — which ignores P2P regressions, so it is an upper bound on the pass
    rate and never a substitute for it. ``--regressions`` lists those trials.
    Returns None for a benchmark reporting neither.
    """
    if not any(_partial_value(outcome) is not None for outcome in outcomes):
        return None

    earned = 0.0
    for outcome in outcomes:
        value = _partial_value(outcome)
        if value is None:
            # Nothing softer to go on; the binary reward is all this trial said.
            value = 1.0 if outcome.solved else 0.0
        earned += value
    return earned / len(outcomes)


def test_rates(outcomes: list[TrialOutcome]) -> tuple[float | None, float | None]:
    """``(P2P, F2P)``: the mean share of each kind of test left green.

    The two halves the binary reward is an AND of, reported apart so a middling
    partial score says which way it fell — an agent that broke the suite reads
    as a low P2P, one that simply missed the bug as a low F2P.

    Averaged per trial rather than over the pooled tests, like
    ``partial_credit``, so a task with a thousand tests does not outweigh a
    hundred with ten. A trial whose verifier counted no tests sits out of both
    means and a trial with no P2P test of its own sits out of that one, so the
    two denominators need not match each other or the pass rate's. Returns
    ``(None, None)`` for a benchmark that counts no tests at all.
    """
    p2p: list[float] = []
    f2p: list[float] = []
    for outcome in outcomes:
        counts = outcome.test_counts
        if counts is None:
            continue
        f2p_passed, f2p_total, p2p_passed, p2p_total = counts
        if f2p_total:
            f2p.append(f2p_passed / f2p_total)
        if p2p_total:
            p2p.append(p2p_passed / p2p_total)
    return (
        sum(p2p) / len(p2p) if p2p else None,
        sum(f2p) / len(f2p) if f2p else None,
    )


def regressions(outcomes: list[TrialOutcome]) -> list[str]:
    """Trials that left a PASS_TO_PASS test red."""
    out = []
    for outcome in outcomes:
        counts = outcome.test_counts
        if counts is not None and counts[3] and counts[2] < counts[3]:
            out.append(f"{outcome.task} ({counts[2]}/{counts[3]} P2P)")
    return out


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def mean_effort(spent: list[Spent]) -> tuple[Effort | None, Effort | None]:
    """``(over every trial, over the solved trials)``.

    A zero counts as unrecorded, not as free: a trial that made no LLM call at
    all lost its sandbox or never started, and would read as an efficient one.
    """

    def means(rows: list[Spent]) -> Effort | None:
        def avg(values) -> float | None:
            kept = [value for value in values if value]
            return sum(kept) / len(kept) if kept else None

        effort = Effort(
            avg(row.turns for row in rows),
            avg(row.input_tokens for row in rows),
            avg(row.output_tokens for row in rows),
        )
        return effort if any(value is not None for value in effort) else None

    return means(spent), means([row for row in spent if row.solved])


def usage_tokens(usage) -> tuple[int | None, int | None]:
    """``(input, output)`` out of a native rollout's ``metrics.usage``."""
    if not isinstance(usage, dict):
        return None, None
    input_tokens = _count(usage.get("input_tokens"))
    if input_tokens is not None:
        # Anthropic's input_tokens leaves the cached prefix out; OpenAI's, which
        # carries cached_input_tokens instead, already counts it.
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            input_tokens += _count(usage.get(key)) or 0
    return input_tokens, _count(usage.get("output_tokens"))


def native_spent(rows: list[dict]) -> list[Spent]:
    """One entry per instance of an ``orchard-eval run``, its last line winning.

    Every harness records ``turns``; only the ones whose CLI reports usage —
    codex, today — record tokens.
    """
    latest = {row.get("instance_id"): row for row in rows}
    out = []
    for row in latest.values():
        metrics = row.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
        out.append(
            Spent(
                bool(row.get("resolved")),
                _count(metrics.get("turns")),
                *usage_tokens(metrics.get("usage")),
            )
        )
    return out


def agent_turns(trial_dir: Path) -> int | None:
    """LLM responses in one Harbor trial, from the agent's own trajectory.

    Harbor's ATIF ``trajectory.json`` has one ``agent`` step per response —
    Claude Code's several log lines per message already merged into one. pi
    writes no ATIF file, only its session log, one line per message.
    """
    agent = trial_dir / "agent"
    try:
        data = json.loads((agent / "trajectory.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = None
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        steps = data.get("steps")
        if not isinstance(steps, list):
            return None
        return sum(
            1 for step in steps if isinstance(step, dict) and step.get("source") == "agent"
        ) or None

    turns = 0
    for path in sorted((agent / "pi" / "sessions").glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message") if isinstance(row, dict) else None
            if isinstance(message, dict) and message.get("role") == "assistant":
                turns += 1
    return turns or None


def replay_source(trial_dir: Path) -> Path | None:
    """The agent trial whose patch a re-grade trial replayed.

    ``replay.json`` names the patch by absolute path. When the results tree has
    been copied elsewhere since, the same path is re-anchored at this trial's
    own ``harbor/`` directory.
    """
    try:
        data = json.loads((trial_dir / "agent" / "replay.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    patch = data.get("patch") if isinstance(data, dict) else None
    if not isinstance(patch, str) or not patch:
        return None
    source = Path(patch).parent.parent  # <trial>/agent/model.patch
    if source.is_dir():
        return source
    parts = source.parts
    if "harbor" not in parts:
        return None
    below = parts[len(parts) - parts[::-1].index("harbor") :]
    # trial_dir is harbor/<job>/<attempt>/<trial>.
    moved = trial_dir.parents[2].joinpath(*below)
    return moved if moved.is_dir() else None


def harbor_spent(outcome: TrialOutcome) -> Spent:
    """One Harbor trial's turns and tokens, a re-grade's taken from its source."""
    trial_dir = outcome.trial_dir
    input_tokens, output_tokens = outcome.input_tokens, outcome.output_tokens
    if trial_dir is not None and input_tokens is None and output_tokens is None:
        source = replay_source(trial_dir)
        if source is not None:
            trial_dir = source
            try:
                data = json.loads((source / "result.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                input_tokens, output_tokens = agent_tokens(data)
    turns = agent_turns(trial_dir) if trial_dir is not None else None
    return Spent(outcome.solved, turns, input_tokens, output_tokens)


def swebench_scores(
    root: Path, keep, want_times: bool, want_effort: bool = True
) -> list[tuple[str, Score]]:
    """One entry per ``orchard-eval run`` directory, newest attempt only.

    ``summary.json`` is written once, at the end. Until then the run's progress
    is in ``results.jsonl``, one line per finished instance — which is what
    makes a mid-run number possible at all.
    """
    out = []
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if run_dir.name == "harbor" or not keep(run_dir.name):
            continue
        # Run ids are YYYYmmdd-HHMMSS, so the last one sorted is the newest.
        attempts = sorted(p for p in run_dir.iterdir() if p.is_dir())
        if not attempts:
            continue
        score = read_attempt(attempts[-1], want_times, want_effort)
        if score is not None:
            out.append((run_dir.name, score))
    return out


def read_attempt(
    attempt: Path, want_times: bool = True, want_effort: bool = True
) -> Score | None:
    summary = attempt / "summary.json"
    records = attempt / "results.jsonl"
    finished = summary.is_file()
    # A finished run's score is in summary.json; its turns and tokens are not.
    rows = read_records(records) if want_effort or not finished else []
    effort = effort_solved = None
    if want_effort and rows:
        effort, effort_solved = mean_effort(native_spent(rows))

    if finished:
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            wall_clock = float(data.get("wall_clock_s") or 0)
            return Score(
                int(data["resolved"]),
                int(data["total"]),
                elapsed_s=wall_clock or (dir_span_s(attempt) if want_times else None),
                effort=effort,
                effort_solved=effort_solved,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"skipping unreadable {summary}: {exc}", file=sys.stderr)
            return None

    if not rows:
        return None
    return Score(
        sum(bool(row.get("resolved")) for row in rows),
        len(rows),
        running=True,
        elapsed_s=dir_span_s(attempt) if want_times else None,
        effort=effort,
        effort_solved=effort_solved,
    )


def read_records(records: Path) -> list[dict]:
    """Every complete line of ``results.jsonl``; empty when there is none."""
    if not records.is_file():
        return []
    rows = []
    try:
        with records.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    # The last line can be half-written while the run appends.
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError as exc:
        print(f"skipping unreadable {records}: {exc}", file=sys.stderr)
        return []
    return rows


def harbor_scores(
    root: Path,
    keep,
    want_times: bool,
    hard_ids: frozenset[str] = frozenset(),
    want_effort: bool = True,
) -> tuple[list[tuple[str, Score]], dict[str, list[str]]]:
    """One entry per Harbor job, plus the P2P regressions each one left.

    Jobs are read concurrently: each is a few thousand small files on a network
    filesystem, so the time is spent waiting on the server, not computing.
    """
    harbor = root / "harbor"
    if not harbor.is_dir():
        return [], {}
    # keep() records what it skips, so it runs here rather than in the pool.
    names = [
        p.name for p in sorted(harbor.iterdir()) if p.is_dir() and keep(p.name)
    ]

    def score_job(name: str):
        # The job directory names the run; the attempt under it holds the
        # trials. Reading one and labelling with the other is deliberate —
        # labelling with the attempt would hand split_run_name a timestamp.
        attempt = newest_attempt(harbor / name)
        outcomes = collect_outcomes(attempt)
        if not outcomes:
            return None
        summary = summarize(outcomes)
        p2p, f2p = test_rates(outcomes)
        hard_solved = hard_total = None
        label = benchmark_label(split_run_name(name)[1])
        if hard_ids and wants_hard_column(label):
            hard = [o for o in outcomes if bare_task_id(o.task) in hard_ids]
            hard_solved, hard_total = sum(o.solved for o in hard), len(hard)
        elapsed = None
        if want_times:
            elapsed = trial_span_s(outcomes) or dir_span_s(attempt)
        effort = effort_solved = None
        if want_effort:
            workers = min(TRAJECTORY_WORKERS, len(outcomes))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                spent = list(pool.map(harbor_spent, outcomes))
            effort, effort_solved = mean_effort(spent)
        score = Score(
            summary["solved"],
            summary["total"],
            partial_credit(outcomes),
            p2p,
            f2p,
            # orchard-eval writes this only once the job is summarized.
            running=not (attempt / "orchard-summary.json").is_file(),
            elapsed_s=elapsed,
            hard_solved=hard_solved,
            hard_total=hard_total,
            effort=effort,
            effort_solved=effort_solved,
        )
        return name, score, regressions(outcomes)

    out = []
    regressed: dict[str, list[str]] = {}
    if not names:
        return out, regressed
    with ThreadPoolExecutor(max_workers=min(JOB_WORKERS, len(names))) as pool:
        for result in pool.map(score_job, names):
            if result is None:
                continue
            name, score, broke = result
            out.append((name, score))
            if broke:
                regressed[name] = broke
    return out, regressed


def build_matrix(
    root: Path,
    include_smoke: bool,
    want_times: bool = True,
    hard_ids: frozenset[str] = frozenset(),
    hidden: frozenset[str] = frozenset(),
    want_effort: bool = True,
):
    """-> (rows, columns, cells, skipped, hidden_jobs, regressions).

    A column is ``(benchmark label, kind)`` where kind is ``rate``, ``hard`` or
    ``partial``. Jobs whose column is in ``hidden`` are never read.
    """
    cells: dict[tuple[str, str], Score] = {}
    harnesses: list[str] = []
    labels: list[str] = []
    skipped: list[str] = []
    hidden_jobs: list[str] = []

    def keep(name: str) -> bool:
        """Decided from the name alone, before anything reads the job's files."""
        if "-smoke" in name and not include_smoke:
            skipped.append(name)
            return False
        harness, benchmark = split_run_name(name)
        if not harness or not benchmark:
            skipped.append(name)
            return False
        if benchmark_label(benchmark) in hidden:
            hidden_jobs.append(name)
            return False
        return True

    harbor, regressed = harbor_scores(root, keep, want_times, hard_ids, want_effort)
    for name, score in swebench_scores(root, keep, want_times, want_effort) + harbor:
        harness, benchmark = split_run_name(name)
        label = benchmark_label(benchmark)
        if harness not in harnesses:
            harnesses.append(harness)
        if label not in labels:
            labels.append(label)
        cells[(harness, label)] = score

    order = COLUMN_ORDER
    labels.sort(key=lambda c: (order.index(c) if c in order else len(order), c))
    harnesses.sort(
        key=lambda h: HARNESSES.index(h) if h in HARNESSES else len(HARNESSES)
    )

    columns: list[tuple[str, str]] = []
    for label in labels:
        columns.append((label, "rate"))
        scored = [score for h in harnesses if (score := cells.get((h, label)))]
        if any(score.hard_total is not None for score in scored):
            columns.append((label, "hard"))
        if any(score.partial is not None for score in scored):
            # Only promise P2P/F2P in the heading if a cell can actually fill it.
            counted = any(
                score.p2p is not None or score.f2p is not None for score in scored
            )
            columns.append((label, "partial+tests" if counted else "partial"))

    return harnesses, columns, cells, skipped, hidden_jobs, regressed


def format_cell(score: Score | None, kind: str, counts: bool) -> str:
    if score is None:
        return EMPTY
    mark = "*" if score.running else ""
    if kind.startswith("partial"):
        if score.partial is None:
            return EMPTY
        body = f"{score.partial * 100:.2f}%"
        if kind == "partial+tests":
            body += f" ({format_share(score.p2p)}, {format_share(score.f2p)})"
        return body + mark
    if kind == "hard":
        if not score.hard_total:
            return EMPTY
        pct = f"{score.hard_solved / score.hard_total * 100:.2f}%"
        body = f"{pct} ({score.hard_solved}/{score.hard_total})" if counts else pct
        return body + mark
    if not score.total:
        return EMPTY
    pct = f"{score.solved / score.total * 100:.2f}%"
    body = f"{pct} ({score.solved}/{score.total})" if counts else pct
    return body + mark


def format_share(value: float | None) -> str:
    return EMPTY if value is None else f"{value * 100:.2f}%"


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return EMPTY
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_tokens(value: float | None) -> str:
    if value is None:
        return EMPTY
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= scale:
            return f"{value / scale:.3g}{suffix}"
    return f"{value:.0f}"


def format_effort(score: Score | None, what: str) -> str:
    """``mean over all trials (mean over solved trials)``, for *what* of
    ``turns`` or ``tokens``."""
    if score is None:
        return EMPTY

    def one(effort: Effort | None) -> str | None:
        if effort is None:
            return None
        if what == "turns":
            return None if effort.turns is None else f"{effort.turns:.1f}"
        if effort.input_tokens is None and effort.output_tokens is None:
            return None
        return (
            f"{format_tokens(effort.input_tokens)}"
            f" / {format_tokens(effort.output_tokens)}"
        )

    body = one(score.effort)
    if body is None:
        return EMPTY
    body += f" ({one(score.effort_solved) or EMPTY})"
    return body + ("*" if score.running else "")


def heading(column: tuple[str, str]) -> str:
    label, kind = column
    if kind == "hard":
        return f"{label} {HARD_SUBSET}"
    if kind == "partial+tests":
        return f"{label} (partial, P2P, F2P)"
    if kind == "partial":
        return f"{label} (partial)"
    return label


def score_grid(title, harnesses, columns, cells, counts):
    header = [title] + [heading(column) for column in columns]
    rows = [
        [harness]
        + [
            format_cell(cells.get((harness, label)), kind, counts)
            for label, kind in columns
        ]
        for harness in harnesses
    ]
    return header, rows


def time_grid(title, harnesses, columns, cells):
    """Wall clock per job, plus each harness's and each benchmark's total."""
    labels = [label for label, kind in columns if kind == "rate"]
    header = [f"{title} — wall clock"] + labels + ["total"]

    rows = []
    for harness in harnesses:
        durations = [
            (cells[(harness, label)].elapsed_s or 0.0)
            if (harness, label) in cells
            else 0.0
            for label in labels
        ]
        rows.append(
            [harness]
            + [
                format_duration(value) if (harness, label) in cells else EMPTY
                for value, label in zip(durations, labels)
            ]
            + [format_duration(sum(durations))]
        )

    column_totals = [
        sum(
            (cells[(harness, label)].elapsed_s or 0.0)
            for harness in harnesses
            if (harness, label) in cells
        )
        for label in labels
    ]
    rows.append(
        ["total"]
        + [format_duration(value) for value in column_totals]
        + [format_duration(sum(column_totals))]
    )
    return header, rows


EFFORT_TITLES = {
    "turns": "mean turns (solved)",
    "tokens": "mean tokens in / out (solved)",
}


def effort_grid(title, harnesses, columns, cells, what):
    """Mean turns or tokens per trial, one cell per job."""
    labels = [label for label, kind in columns if kind == "rate"]
    header = [f"{title} — {EFFORT_TITLES[what]}"] + labels
    rows = [
        [harness] + [format_effort(cells.get((harness, label)), what) for label in labels]
        for harness in harnesses
    ]
    return header, rows


def render_grid(header, rows, fmt) -> str:
    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows([[c if c != EMPTY else "" for c in row] for row in rows])
        return buf.getvalue().rstrip("\n")

    widths = [
        max(len(header[i]), *(len(row[i]) for row in rows)) if rows else len(header[i])
        for i in range(len(header))
    ]

    def line(cols):
        padded = [cols[0].ljust(widths[0])] + [
            cols[i].rjust(widths[i]) for i in range(1, len(cols))
        ]
        return "| " + " | ".join(padded) + " |"

    if fmt == "markdown":
        sep = (
            "|"
            + "|".join(
                (":" + "-" * (w + 1)) if i == 0 else ("-" * (w + 1) + ":")
                for i, w in enumerate(widths)
            )
            + "|"
        )
        return "\n".join([line(header), sep] + [line(row) for row in rows])

    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    return "\n".join([rule, line(header), rule] + [line(row) for row in rows] + [rule])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "results",
        nargs="*",
        help="One or more results/<MODEL_TAG> directories (default results/$MODEL_TAG)",
    )
    parser.add_argument(
        "--counts", action="store_true", help="Show solved/total beside the rate"
    )
    parser.add_argument(
        "--include-smoke", action="store_true", help="Include -smoke<N> limited runs"
    )
    parser.add_argument(
        "--regressions",
        action="store_true",
        help="List the trials that left a PASS_TO_PASS test red",
    )
    parser.add_argument(
        "--no-times",
        action="store_true",
        help="Skip the wall-clock table, and the file walk that dates it",
    )
    parser.add_argument(
        "--no-efficiency",
        action="store_true",
        help="Skip the turns and tokens tables, and the trajectory reads behind them",
    )
    parser.add_argument(
        "--hard51-file",
        type=Path,
        default=None,
        help="SWE-bench Pro V2 HARD-51 id list"
        " (default $PRO_V2_DIR/v2/hard51_ids.txt)",
    )
    parser.add_argument(
        "--all-benchmarks",
        action="store_true",
        help="Also show the columns hidden by default: "
        + ", ".join(sorted(HIDDEN_BY_DEFAULT)),
    )
    parser.add_argument(
        "--format", choices=("table", "markdown", "csv"), default="table"
    )
    args = parser.parse_args()
    hidden = frozenset() if args.all_benchmarks else HIDDEN_BY_DEFAULT

    hard_file = args.hard51_file or default_hard_file()
    hard_ids = read_hard_ids(hard_file)
    if args.hard51_file and not hard_ids:
        parser.error(f"no task ids read from --hard51-file {hard_file}")

    roots = [Path(p) for p in args.results]
    if not roots:
        tag = os.environ.get("MODEL_TAG")
        if not tag:
            parser.error("pass a results directory, or set MODEL_TAG")
        roots = [Path("results") / tag]

    for root in roots:
        if not root.is_dir():
            print(f"no such directory: {root}", file=sys.stderr)
            continue

        harnesses, columns, cells, skipped, hidden_jobs, regressed = build_matrix(
            root,
            args.include_smoke,
            not args.no_times,
            hard_ids,
            hidden,
            not args.no_efficiency,
        )
        if not cells:
            print(f"no completed runs under {root}", file=sys.stderr)
            continue

        print()
        header, rows = score_grid(root.name, harnesses, columns, cells, args.counts)
        print(render_grid(header, rows, args.format))

        if not args.no_times:
            print()
            header, rows = time_grid(root.name, harnesses, columns, cells)
            print(render_grid(header, rows, args.format))

        if not args.no_efficiency:
            for what in ("turns", "tokens"):
                print()
                header, rows = effort_grid(root.name, harnesses, columns, cells, what)
                print(render_grid(header, rows, args.format))

        if args.format == "csv":
            continue

        print()
        if any(kind == "hard" for _label, kind in columns):
            print(
                f"  {HARD_SUBSET}: the same job's trials restricted to the"
                f" {len(hard_ids)} ids in {hard_file}."
            )
        elif not hard_ids and any(
            wants_hard_column(label) for label, _kind in columns
        ):
            print(
                f"  {HARD_SUBSET}: not shown — no ids at {hard_file};"
                " set PRO_V2_DIR or pass --hard51-file."
            )
        if any(kind.startswith("partial") for _label, kind in columns):
            print(
                "  (partial) mean of the verifier's own partial score — the"
                " Partial column of Harbor's summary, not its F2P one."
            )
        if any(kind == "partial+tests" for _label, kind in columns):
            print(
                "  (P2P, F2P) beside it: mean share of PASS_TO_PASS and of"
                " FAIL_TO_PASS tests green, averaged per trial over the trials"
                " whose verifier counted them."
            )
        if not args.no_efficiency:
            print(
                "  turns: LLM responses per trial; tokens: input / output summed"
                " over those calls, input re-counting the whole context each"
                " time. Mean over the trials that recorded them, then (over the"
                " solved ones). A re-grade reports the run whose patch it replayed."
            )
        running = sorted(
            f"{h} x {label}" for (h, label), score in cells.items() if score.running
        )
        if running:
            print(
                f"  * still running, scored over completed work only:"
                f" {', '.join(running)}"
            )
        missing = [
            f"{h} x {label}"
            for h in harnesses
            for label, kind in columns
            if kind == "rate" and (h, label) not in cells
        ]
        if missing:
            print(f"  not run: {', '.join(missing)}")
        if skipped:
            print(f"  ignored: {', '.join(sorted(skipped))}")
        if hidden_jobs:
            print(
                f"  hidden (--all-benchmarks to show): {', '.join(sorted(hidden_jobs))}"
            )
        if regressed:
            total = sum(len(v) for v in regressed.values())
            print(f"  P2P regressions: {total} trial(s) across {len(regressed)} job(s)")
            if args.regressions:
                for job, tasks in sorted(regressed.items()):
                    for task in tasks:
                        print(f"      {job}: {task}")
        print(f"  source:  {root}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
