"""The turns and tokens tables of ``scripts/results_table.py``.

``scripts/`` is not a package, so the module is loaded by path — the same thing
``python scripts/results_table.py`` does, without the subprocess.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "results_table.py"


def _load():
    spec = importlib.util.spec_from_file_location("results_table", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


results_table = _load()

ATTEMPT = "20261006-000000"


def write_trial(
    harbor: Path,
    job: str,
    name: str,
    reward: float,
    *,
    tokens=(None, None),
    steps: int | None = None,
    pi_turns: int | None = None,
    replays: str | None = None,
) -> Path:
    trial = harbor / job / ATTEMPT / name
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": f"bench/{name.split('__')[0]}",
                "trial_name": name,
                "verifier_result": {"rewards": {"reward": reward}},
                "agent_result": {
                    "n_input_tokens": tokens[0],
                    "n_output_tokens": tokens[1],
                },
            }
        )
    )
    if steps is not None:
        trajectory = [{"source": "system"}, {"source": "user"}]
        trajectory += [{"source": "agent"}] * steps
        (trial / "agent" / "trajectory.json").write_text(
            json.dumps({"steps": trajectory})
        )
    if pi_turns is not None:
        sessions = trial / "agent" / "pi" / "sessions"
        sessions.mkdir(parents=True)
        roles = ["user"] + ["assistant", "toolResult"] * pi_turns
        (sessions / "s.jsonl").write_text(
            "\n".join(json.dumps({"message": {"role": role}}) for role in roles)
        )
    if replays is not None:
        # Recorded on another machine: the absolute path no longer exists here.
        patch = f"/elsewhere/results/M/harbor/{replays}/agent/model.patch"
        (trial / "agent" / "replay.json").write_text(json.dumps({"patch": patch}))
    return trial


class TestAgentTurns:
    def test_counts_the_agent_steps_of_the_trajectory(self, tmp_path):
        trial = write_trial(tmp_path, "j", "a__1", 1, steps=110)
        assert results_table.agent_turns(trial) == 110

    def test_counts_pi_assistant_messages(self, tmp_path):
        trial = write_trial(tmp_path, "j", "a__1", 1, pi_turns=40)
        assert results_table.agent_turns(trial) == 40

    def test_nothing_recorded_is_none(self, tmp_path):
        trial = write_trial(tmp_path, "j", "a__1", 1)
        assert results_table.agent_turns(trial) is None


class TestMeanEffort:
    def test_zero_is_unrecorded_and_solved_are_averaged_apart(self):
        Spent = results_table.Spent
        effort, solved = results_table.mean_effort(
            [
                Spent(True, 10, 1000, 10),
                Spent(False, 30, 3000, 30),
                Spent(False, 0, 0, 0),
                Spent(False, None, None, None),
            ]
        )
        assert effort == (20.0, 2000.0, 20.0)
        assert solved == (10.0, 1000.0, 10.0)

    def test_no_solved_trial_is_none(self):
        _effort, solved = results_table.mean_effort([results_table.Spent(False, 5)])
        assert solved is None


class TestUsageTokens:
    def test_openai_input_already_counts_its_cache(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 9}
        assert results_table.usage_tokens(usage) == (100, 9)

    def test_anthropic_input_gets_its_cache_added(self):
        usage = {
            "input_tokens": 10,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 10,
            "output_tokens": 9,
        }
        assert results_table.usage_tokens(usage) == (100, 9)

    def test_missing_usage(self):
        assert results_table.usage_tokens(None) == (None, None)


class TestMatrix:
    @pytest.fixture
    def cells(self, tmp_path):
        root = tmp_path / "M"
        harbor = root / "harbor"
        solve = "mini-swe-agent-swebench-pro-v2"
        write_trial(harbor, solve, "a__1", 1, tokens=(9_000_000, 90_000), steps=110)
        write_trial(harbor, solve, "b__2", 0, tokens=(4_000_000, 30_000), steps=50)
        regrade = "mini-swe-agent-swebench-pro-v2-regrade"
        # The re-grade flips both verdicts; the effort is still its source's.
        write_trial(harbor, regrade, "a__9", 0, replays=f"{solve}/{ATTEMPT}/a__1")
        write_trial(harbor, regrade, "b__9", 1, replays=f"{solve}/{ATTEMPT}/b__2")
        for job in (solve, regrade):
            (harbor / job / ATTEMPT / "orchard-summary.json").write_text("{}")

        native = root / "codex-swebench-verified" / "20260927-232350"
        native.mkdir(parents=True)
        rows = [
            {
                "instance_id": "i1",
                "resolved": True,
                "metrics": {
                    "turns": 5,
                    "usage": {"input_tokens": 50_000, "output_tokens": 1_000},
                },
            },
            {"instance_id": "i2", "resolved": False, "metrics": {"turns": 15}},
        ]
        (native / "results.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        (native / "summary.json").write_text(json.dumps({"resolved": 1, "total": 2}))

        _harnesses, _columns, cells, *_ = results_table.build_matrix(
            root, include_smoke=False, want_times=False
        )
        return cells

    def test_harbor_job(self, cells):
        score = cells[("mini-swe-agent", "SWE-bench Pro V2 (Harbor)")]
        assert score.effort == (80.0, 6_500_000.0, 60_000.0)
        assert score.effort_solved == (110.0, 9_000_000.0, 90_000.0)

    def test_regrade_inherits_from_the_trial_it_replayed(self, cells):
        score = cells[("mini-swe-agent", "SWE-bench Pro V2 (re-grade)")]
        assert score.effort == (80.0, 6_500_000.0, 60_000.0)
        assert score.effort_solved == (50.0, 4_000_000.0, 30_000.0)

    def test_finished_native_run_reads_its_records(self, cells):
        score = cells[("codex", "SWE-bench Verified")]
        assert score.effort == (10.0, 50_000.0, 1_000.0)
        assert score.effort_solved == (5.0, 50_000.0, 1_000.0)

    def test_cells(self, cells):
        score = cells[("mini-swe-agent", "SWE-bench Pro V2 (re-grade)")]
        assert results_table.format_effort(score, "turns") == "80.0 (50.0)"
        assert results_table.format_effort(score, "tokens") == "6.5M / 60K (4M / 30K)"


class TestHard51Runs:
    """A PRO_V2_HARD51=1 job gets columns of its own beside the full set's."""

    @pytest.fixture
    def matrix(self, tmp_path):
        root = tmp_path / "M"
        harbor = root / "harbor"
        full = "mini-swe-agent-swebench-pro-v2-regrade"
        write_trial(harbor, full, "a__1", 1)
        write_trial(harbor, full, "b__1", 0)
        write_trial(harbor, full, "c__1", 1)
        hard = "mini-swe-agent-swebench-pro-v2-hard51"
        write_trial(harbor, hard, "a__2", 0)
        hard_regrade = "mini-swe-agent-swebench-pro-v2-hard51-regrade"
        write_trial(harbor, hard_regrade, "a__3", 1)
        _harnesses, columns, cells, *_ = results_table.build_matrix(
            root,
            include_smoke=False,
            want_times=False,
            hard_ids=frozenset({"a", "b"}),
            want_effort=False,
        )
        return columns, cells

    def test_does_not_take_over_the_full_sets_cell(self, matrix):
        _columns, cells = matrix
        full = cells[("mini-swe-agent", "SWE-bench Pro V2 (re-grade)")]
        assert (full.solved, full.total) == (2, 3)
        assert (full.hard_solved, full.hard_total) == (1, 2)

    def test_scored_in_its_own_columns(self, matrix):
        _columns, cells = matrix
        regrade = cells[("mini-swe-agent", "SWE-bench Pro V2 HARD-51 (re-grade)")]
        assert (regrade.solved, regrade.total) == (1, 1)
        direct = cells[("mini-swe-agent", "SWE-bench Pro V2 HARD-51 (Harbor)")]
        assert (direct.solved, direct.total) == (0, 1)

    def test_gets_no_redundant_hard_sub_column(self, matrix):
        columns, _cells = matrix
        hard_columns = [label for label, kind in columns if kind == "hard"]
        assert hard_columns == ["SWE-bench Pro V2 (re-grade)"]
