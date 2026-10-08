#!/usr/bin/env bash
# Evaluate a model on SWE-bench Pro V2 — 642 tasks, air-gapped agent phase,
# graded by replaying the agent's patch into a fresh sandbox.
#
# V2 exists because V1 leaks its own answer. V1's images ship a full clone whose
# history contains the fixing commit, and the task *name* carries its SHA, so
# `git show <that sha> | git apply` is a complete solution. On the 2026-09-21
# sweep 701 of 731 trials read that history; the 219 that copied gold into a
# non-test source file scored 90% against 71% for the trials that never saw it.
# V2 rebuilds every image from a sanitised bundle — no fixing commit, no stray
# refs, stashes or hooks — and restores tests with `git apply
# /tests/test_patch.patch` instead of checking them out of the fix commit.
#
# Three things about this path differ from `run_swebench_pro_harbor.sh`, and
# getting any of them wrong produces a number rather than an error:
#
#   1. NO `ORCHARD_HARBOR_FORCE_ISOLATION`. V1 needed it because all 731 tasks
#      declared `allow_internet = true` and nothing air-gapped the pod. V2
#      declares the air-gap itself — `[agent] network_mode = "no-network"` on
#      every task — and its `[verifier]` is deliberately left on the public
#      baseline, because some Go tasks download modules while testing. Forcing
#      isolation here does not harden anything; it breaks grading.
#
#      This makes V2 the first dataset that asks the provider to *restore*
#      network after the agent phase (public -> no-network -> public). DeepSWE
#      stays isolated through its verifier, so that direction is exercised for
#      the first time here. The oracle check below is what proves it works.
#
#   2. The budget is the task's own 3000s, not the house 1.5x multiplier.
#      Upstream's locked protocol is a 50-minute cap, and a number measured
#      against a longer one is not comparable with theirs. It also keeps agent
#      plus verifier (3000 + 3000) inside ORCHARD_HARBOR_EXEC_TIMEOUT and the
#      orchestrator's 2h SANDBOX_TTL_HOURS, which 4500 + 3000 would not.
#
#   3. The number to report is the *re-graded* one. The verifier still runs in
#      the agent's own container, so it still sees whatever the agent left
#      behind. REGRADE= replays each trial's captured `model.patch` into a
#      clean image and grades that instead. Publish both, as upstream asks.
#
# The pod is still overridden to 4 CPU / 16 GiB against the task's declared
# 1 / 4096. That is a deliberate deviation from upstream: 1 CPU on element-web
# or teleport turns a build into a timeout, and a timeout reads as a model
# failure. Set OVERRIDE_CPUS/OVERRIDE_MEMORY_MB to 1/4096 for exact parity with
# the published leaderboard.
#
# `ORCHARD_HARBOR_PREFER_IPV4` (on by default) is the other deliberate
# deviation, and it is the same kind: the pod grading itself rather than the
# model. Several NodeBB tasks write `{"url":"http://localhost:4568"}` and then
# start a server that binds `0.0.0.0` — not `::1`. Upstream's single-stack
# containers make `localhost` mean 127.0.0.1 for free, because glibc's
# AI_ADDRCONFIG drops a v6 answer from a host with no global v6 address. This
# cluster's pods are dual-stack, so `::1` survives, RFC 3484 sorts it first, and
# the suite fails with ECONNREFUSED against a listener that is running. Both
# zeros on the 2026-09-24 oracle run were exactly that. See
# harbor_orchard/README.md.
#
# Sampling, because this is the gate that catches all of the above: `--limit N`
# is Harbor's `n_tasks`, which takes the **first N of the sorted task list**,
# not a sample. NodeBB sorts first, so `--limit 25` measures NodeBB's oracle
# rate on 1 of 11 repos. Use `SAMPLE=N` instead — it round-robins the repos, so
# 25 tasks cover all 11.
#
# Usage:
#   ./scripts/fetch_swebench_pro_v2.sh                       # once, first
#
#   SAMPLE=25 ./scripts/run_swebench_pro_v2.sh oracle 16 --threshold 1.0
#   SAMPLE=25 ./scripts/run_swebench_pro_v2.sh nop    16 --max-solve-rate 0.0
#   ./scripts/run_swebench_pro_v2.sh mini-swe-agent 24
#   HARD51=1 ./scripts/run_swebench_pro_v2.sh codex 16       # the HARD-51 subset
#   TASK_IDS=rerun.txt ./scripts/run_swebench_pro_v2.sh mini-swe-agent 24
#   REGRADE=results/harbor/mini-...-v2/20260923-101500 \
#       ./scripts/run_swebench_pro_v2.sh mini-swe-agent 24
#
# Arguments after the concurrency go to `orchard-eval harbor`; anything after a
# bare `--` is forwarded verbatim to `harbor run`.
#
# Environment:
#   PRO_V2_DIR         The upstream checkout. Default ./third_party/SWE-bench_Pro-os
#   SANDBOX_BASE_URL   required — your orchestrator
#   SANDBOX_API_KEY    required if the orchestrator enforces auth
#   MODEL_NAME         required for every agent but oracle/nop/replay
#   MODEL_BASE_URL     OpenAI-compatible /v1 root. Its host:port is the *only*
#                      destination an isolated pod can reach.
#   MODEL_BASE_URL_REPLICAS / MODEL_ROUTING / API_KEY / MODEL_ID / ATTEMPTS
#                      As in run_swebench_pro_harbor.sh.
#   AGENT_TIMEOUT_MULTIPLIER
#                      Defaults to 1.0. Raising it leaves the locked protocol.
#   HARD51=1           Run only the 51 tasks upstream found discriminating.
#   TASK_IDS=<list>    Run a named subset: either a path to a file of instance
#                      ids (one per line, `#` comments allowed) or an inline
#                      comma-separated list. Entries are normalised to the
#                      glob Harbor matches with, so a bare id pasted out of a
#                      results table works. An id that is not in the task tree
#                      is an error rather than a silently smaller run. Use it
#                      to re-run the trials a run could not measure — the ones
#                      `harbor_orchard.agents` failed with
#                      DirtyWorkingTreeError, or whatever
#                      `scripts/harbor_failed_tasks.py` printed. Cannot be
#                      combined with HARD51= or SAMPLE=.
#   SAMPLE=<n>         Run n tasks spread evenly across the 11 repos. This is
#                      what `--limit n` is not: that one takes the first n of a
#                      sorted list, which is all NodeBB.
#   ORCHARD_HARBOR_PREFER_IPV4=0
#                      Leave the pod's resolver as the orchestrator built it.
#                      Expect NodeBB ECONNREFUSED failures on a dual-stack pod.
#   REGRADE=<job dir>  Replay that job's model.patch files in fresh sandboxes
#                      instead of running an agent. Refuses to start while that
#                      job is still running, because a trial with no patch yet
#                      is scored zero rather than skipped.
#   REGRADE_ALLOW_INCOMPLETE=1
#                      Re-grade a partial job anyway — a cancelled run, a
#                      sample. Read the score over the finished trials the
#                      guard prints, not over the whole task list.
#   OVERRIDE_CPUS / OVERRIDE_MEMORY_MB
#                      Pod size. Default 4 / 16384; 1 / 4096 is upstream parity.
set -euo pipefail

AGENT="${1:-mini-swe-agent}"
CONCURRENCY="${2:-24}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRO_V2_DIR="${PRO_V2_DIR:-${SCRIPT_DIR}/../third_party/SWE-bench_Pro-os}"
TASKS_DIR="${PRO_V2_DIR}/v2/tasks"

if [[ ! -d "${TASKS_DIR}" ]]; then
    echo "No V2 task tree at ${TASKS_DIR}." >&2
    echo "Fetch and verify it first:" >&2
    echo "  ./scripts/fetch_swebench_pro_v2.sh" >&2
    exit 1
fi
PRO_V2_DIR="$(cd "${PRO_V2_DIR}" && pwd)"
TASKS_DIR="${PRO_V2_DIR}/v2/tasks"

ATTEMPTS="${ATTEMPTS:-1}"
JOBS_DIR="${JOBS_DIR:-./results/harbor}"

# Parity with the other Pro stages, not with the task's own declaration. See
# the header.
OVERRIDE_CPUS="${OVERRIDE_CPUS:-4}"
OVERRIDE_MEMORY_MB="${OVERRIDE_MEMORY_MB:-16384}"

# The task's 3000s for the agent and another 3000s for the verifier, in one
# pod. Capped by the orchestrator's SANDBOX_TTL_HOURS (2h) regardless.
export ORCHARD_HARBOR_EXEC_TIMEOUT="${ORCHARD_HARBOR_EXEC_TIMEOUT:-7200}"
# V2's images are several GB and come from ghcr.io. If trials start failing at
# creation rather than at the agent, this is the number to raise — and
# scripts/mirror_deep_swe_images.sh is the template for mirroring them, which
# is what DeepSWE needed when ECR's anonymous quota throttled it.
export ORCHARD_HARBOR_CREATE_TIMEOUT="${ORCHARD_HARBOR_CREATE_TIMEOUT:-1800}"

: "${SANDBOX_BASE_URL:?SANDBOX_BASE_URL must point at your orchestrator}"

if ! command -v harbor >/dev/null 2>&1; then
    echo "The 'harbor' CLI is not on PATH. From the repository root:" >&2
    echo "  python -m pip install harbor" >&2
    echo "  python -m pip install -e orchard_env -e orchard_eval/harbor_orchard" >&2
    exit 1
fi

# harbor_orchard is imported by the harbor process, not by this script. The cd
# keeps orchard_eval/harbor_orchard/ — a project directory, not a package — off
# sys.path, where it would answer the import with an empty namespace package.
if ! (cd /tmp && python -c "import harbor_orchard as m; assert m.__file__") 2>/dev/null; then
    echo "harbor_orchard is not importable from the environment 'harbor' runs in." >&2
    echo "  python -m pip install -e orchard_env -e orchard_eval/harbor_orchard" >&2
    exit 1
fi

MODEL_ARGS=()
AGENT_ARG="${AGENT}"
EXTRA_ENV=()
LABEL="${AGENT}"

if [[ -n "${REGRADE:-}" ]]; then
    # The re-grade pass. `patch_replay:PatchReplayAgent` is upstream's, loaded
    # off the checkout rather than vendored: it is part of the protocol, and a
    # copy of it here would be one more thing to keep in step with the tasks it
    # grades. For each task it globs <source_job>/instance_*/result.json, takes
    # the one whose `task_name` matches, and replays the `model.patch` beside
    # it — the layout harbor archives /logs/agent into, and which
    # harbor_orchard.agents._ModelPatchCapture is what puts a file in.
    if [[ ! -d "${REGRADE}" ]]; then
        echo "REGRADE='${REGRADE}' is not a directory." >&2
        echo "Point it at a job *attempt* directory — the one holding instance_*/ —" >&2
        echo "e.g. results/harbor/mini-swe-agent-swebench-pro-v2/20260923-101500" >&2
        exit 1
    fi
    REGRADE="$(cd "${REGRADE}" && pwd)"
    if ! compgen -G "${REGRADE}/instance_*/agent/model.patch" >/dev/null; then
        echo "No instance_*/agent/model.patch under ${REGRADE}." >&2
        echo "Nothing to replay: every trial would score zero. Check that the" >&2
        echo "source run used this repo's agents (the capture lives in" >&2
        echo "harbor_orchard.agents) and that ORCHARD_HARBOR_STOCK_AGENTS was unset." >&2
        exit 1
    fi

    # A source job that is still running does not re-grade low — it re-grades
    # *wrong*, and silently. A task the replay cannot find a patch for is not
    # skipped: it applies nothing, runs the verifier against a pristine tree
    # and scores zero, indistinguishable in the results from a patch that was
    # replayed and failed.
    #
    # Measured on the 2026-09-24 sweep, where a replay was launched two hours
    # before the agent job finished: 110 of 642 trials wrote
    # `{"patch": null}` into agent/replay.json and took a forced zero. Every
    # one of them was in the alphabetical tail the agent had not reached yet —
    # qutebrowser 70 of 73, protonmail 22 of 55, tutao 3 of 4, navidrome 1 of
    # 52, and none at all in the other seven repositories. The run reported
    # 501/641 = 78.2% against 501/531 = 94.4% over the trials that actually
    # had a patch to replay, which reads as a 16-point verifier-tampering
    # finding and is nothing of the sort.
    #
    # result.json is the marker to count, because it is what _find_patch
    # matches on. Harbor creates a trial directory when the trial starts and
    # writes result.json when it ends, so the two counts differ for exactly as
    # long as the source job is in flight.
    SRC_TRIALS="$(find "${REGRADE}" -mindepth 1 -maxdepth 1 -type d -name 'instance_*' | wc -l | tr -d ' ')"
    SRC_DONE="$(find "${REGRADE}" -mindepth 2 -maxdepth 2 -path '*/instance_*/result.json' | wc -l | tr -d ' ')"
    if [[ "${SRC_TRIALS}" != "${SRC_DONE}" ]]; then
        echo "The source job is not finished: ${SRC_DONE} of ${SRC_TRIALS} trials" >&2
        echo "under ${REGRADE}" >&2
        echo "have written result.json." >&2
        echo >&2
        echo "Replaying now would score the remaining $((SRC_TRIALS - SRC_DONE)) zero — not" >&2
        echo "because their patches failed, but because there is nothing yet to replay." >&2
        echo "Wait for the agent pass to finish and run this again." >&2
        echo >&2
        echo "To re-grade a partial job deliberately — a cancelled run, a sample —" >&2
        echo "set REGRADE_ALLOW_INCOMPLETE=1 and read the score over the ${SRC_DONE}" >&2
        echo "finished trials rather than over all ${SRC_TRIALS}." >&2
        [[ "${REGRADE_ALLOW_INCOMPLETE:-0}" == "1" ]] || exit 1
        echo >&2
        echo "REGRADE_ALLOW_INCOMPLETE=1: continuing anyway." >&2
    fi

    # Finished, but with no patch beside it. These are real zeros rather than
    # an ordering mistake — the capture is best-effort by construction, see
    # harbor_orchard.agents._ModelPatchCapture — but they are indistinguishable
    # from failed replays in the results, so say how many there are up front.
    SRC_PATCHED="$(find "${REGRADE}" -mindepth 3 -maxdepth 3 -path '*/instance_*/agent/model.patch' -size +0 | wc -l | tr -d ' ')"
    if [[ "${SRC_PATCHED}" -lt "${SRC_DONE}" ]]; then
        echo "NOTE: $((SRC_DONE - SRC_PATCHED)) of ${SRC_DONE} finished trials have no non-empty" >&2
        echo "      model.patch and will score zero without replaying anything. Look for" >&2
        echo "      'model.patch bytes:' in their trial logs." >&2
    fi
    AGENT_ARG="patch_replay:PatchReplayAgent"
    MODEL_ARGS=(--model replay)
    LABEL="${AGENT}-regrade"
    # Nothing in a replay trial calls a model, so leave no route out for one.
    export ORCHARD_HARBOR_MODEL_EGRESS="${ORCHARD_HARBOR_MODEL_EGRESS:-0}"
    export PYTHONPATH="${PRO_V2_DIR}/v2/tooling${PYTHONPATH:+:${PYTHONPATH}}"
    EXTRA_ENV+=(--ak "source_job=${REGRADE}")
elif [[ "${AGENT}" == "oracle" || "${AGENT}" == "nop" ]]; then
    export ORCHARD_HARBOR_MODEL_EGRESS="${ORCHARD_HARBOR_MODEL_EGRESS:-0}"
else
    : "${MODEL_NAME:?MODEL_NAME must be set for agent '${AGENT}' (use oracle for the ceiling check)}"
    # See run_swebench_pro_harbor.sh for why each agent wants a different
    # spelling of --model; MODEL_ID overrides all of it.
    if [[ "${AGENT}" == "pi" ]]; then
        DEFAULT_MODEL="orchard/orchard-model"
    elif [[ "${AGENT}" == "claude-code" ]]; then
        DEFAULT_MODEL="anthropic/${MODEL_NAME}"
    else
        DEFAULT_MODEL="openai/${MODEL_NAME}"
    fi
    MODEL_ARGS=(--model "${MODEL_ID:-${DEFAULT_MODEL}}")
    if [[ -n "${MODEL_BASE_URL:-}" ]]; then
        export OPENAI_BASE_URL="${OPENAI_BASE_URL:-${MODEL_BASE_URL}}"
        export OPENAI_API_BASE="${OPENAI_API_BASE:-${MODEL_BASE_URL}}"
    fi
    if [[ -n "${API_KEY:-}" ]]; then
        export OPENAI_API_KEY="${OPENAI_API_KEY:-${API_KEY}}"
    fi
    export OPENAI_API_KEY="${OPENAI_API_KEY:-EMPTY}"
    export MSWEA_API_KEY="${MSWEA_API_KEY:-${OPENAI_API_KEY}}"
fi

JOB_NAME="${JOB_NAME:-${LABEL}-swebench-pro-v2-$(date +%Y%m%d-%H%M%S)}"

TASK_ARGS=()
if [[ "${HARD51:-0}" == "1" ]]; then
    HARD51_FILE="${PRO_V2_DIR}/v2/hard51_ids.txt"
    [[ -f "${HARD51_FILE}" ]] || { echo "No ${HARD51_FILE}" >&2; exit 1; }
    # A directory of 51 symlinks rather than a --task-file: Harbor's name
    # filter resolves every task path once per pattern, 642 x 51 lookups that
    # take 11-12 minutes on NFS. See stage_task_subset.sh.
    bash "${SCRIPT_DIR}/stage_task_subset.sh" "${TASKS_DIR}" "${HARD51_FILE}" "${PRO_V2_DIR}/v2/hard51/tasks"
    TASKS_DIR="${PRO_V2_DIR}/v2/hard51/tasks"
fi
[[ -n "${TASK_FILE:-}" ]] && TASK_ARGS+=(--task-file "${TASK_FILE}")
[[ -n "${EXCLUDE_TASK_FILE:-}" ]] && TASK_ARGS+=(--exclude-task-file "${EXCLUDE_TASK_FILE}")

# `--include-task-name` is an fnmatch pattern, and what it is matched against
# depends on how the dataset was addressed: the task directory's basename for a
# `--path` dataset (`LocalTaskId.get_name()`, which is this script's case), the
# fully-qualified `<dataset>/<instance>` name for a downloaded one. A leading
# `*` matches both, so every entry gets one — a bare id pasted out of a results
# table and a glob from an earlier list then behave the same way.
normalize_task_ids() {
    awk '
        { sub(/\r$/, ""); gsub(/^[ \t]+|[ \t]+$/, "") }
        /^#/ || /^$/ { next }
        /^\*/ { print; next }
        { print "*" $0 }
    ' "$1"
}

# Re-running a named subset is the other half of the clean-tree guard in
# `harbor_orchard.agents`: the guard turns a pod that arrived used into a failed
# trial, and this is how those trials are run again without re-running 642.
TASK_IDS_FILE=""
if [[ -n "${TASK_IDS:-}" ]]; then
    if [[ "${HARD51:-0}" == "1" || -n "${SAMPLE:-}" ]]; then
        echo "TASK_IDS= cannot be combined with HARD51= or SAMPLE=." >&2
        echo "Harbor ORs its --include-task-name patterns, so the run would be" >&2
        echo "the union of the two subsets rather than the intersection." >&2
        exit 1
    fi
    TASK_IDS_FILE="${TASK_IDS_FILE_PATH:-${TMPDIR:-/tmp}/swebench-pro-v2-ids-${JOB_NAME}.txt}"
    if [[ -f "${TASK_IDS}" ]]; then
        normalize_task_ids "${TASK_IDS}" > "${TASK_IDS_FILE}"
    else
        printf '%s\n' "${TASK_IDS//[, ]/$'\n'}" | normalize_task_ids /dev/stdin \
            > "${TASK_IDS_FILE}"
    fi
    WANTED="$(wc -l < "${TASK_IDS_FILE}" | tr -d ' ')"
    if [[ "${WANTED}" -eq 0 ]]; then
        echo "TASK_IDS= selected no tasks. Give a file of instance ids, one per" >&2
        echo "line, or an inline comma-separated list." >&2
        exit 1
    fi
    # Harbor raises only when *nothing* matches, so a list of 109 with one typo
    # in it would run 108 and report a solve rate over the wrong denominator.
    UNKNOWN=""
    while IFS= read -r entry; do
        id="${entry#\*}"
        [[ "${id}" == */* || "${id}" == *[\*\?]* ]] && continue
        [[ -d "${TASKS_DIR}/${id}" ]] || UNKNOWN="${UNKNOWN}${id}"$'\n'
    done < "${TASK_IDS_FILE}"
    if [[ -n "${UNKNOWN}" ]]; then
        echo "TASK_IDS= names $(printf '%s' "${UNKNOWN}" | wc -l | tr -d ' ') id(s) that are not in ${TASKS_DIR}:" >&2
        printf '%s' "${UNKNOWN}" | head -10 | sed 's/^/  /' >&2
        [[ -f "${TASK_IDS}" ]] || echo "  (TASK_IDS was read as an inline list; pass a path if you meant a file)" >&2
        exit 1
    fi
    TASK_ARGS+=(--task-file "${TASK_IDS_FILE}")
fi

# A gate has to look like the dataset, and `--limit` does not: it becomes
# Harbor's `n_tasks`, which slices the first N off a sorted list. The first 38
# entries are all NodeBB, so `--limit 25` measures one repo of eleven — which is
# how the 2026-09-24 oracle run reported 92% for a dataset-wide failure that is
# specific to NodeBB's test harness.
#
# Round-robin instead: rank each task within its own org, then sort by rank, so
# the first 11 are one per repo, the next 11 are the second of each, and any N
# is as close to proportional as N allows. Deterministic, so two runs of
# `SAMPLE=25` grade the same 25 tasks and their numbers can be compared.
#
# Entries are globs (`*instance_...`) because `--include-task-name` matches the
# task's fully-qualified name, and the dataset prefix belongs to Harbor.
SAMPLE_FILE=""
if [[ -n "${SAMPLE:-}" ]]; then
    # A stable path rather than mktemp: the list is a function of the task tree
    # and N, the last line of this script is an `exec` that would skip any
    # cleanup trap anyway, and leaving it on disk is what lets a later run say
    # exactly which tasks a number came from.
    # `awk NR<=n`, not `head -n`: head exits after n lines while sort still has
    # ~78 KB to write, sort dies of SIGPIPE (141), and under pipefail + set -e
    # the script exits right here without printing a word. awk drains the pipe.
    SAMPLE_FILE="${SAMPLE_FILE_PATH:-${TMPDIR:-/tmp}/swebench-pro-v2-sample-${SAMPLE}.txt}"
    find "${TASKS_DIR}" -mindepth 1 -maxdepth 1 -type d -name 'instance_*' \
        -exec basename {} \; \
        | LC_ALL=C sort \
        | awk -F'__' '{ printf "%06d\t%s\t*%s\n", ++seen[$1], $1, $0 }' \
        | LC_ALL=C sort -k1,1 -k2,2 \
        | awk -v n="${SAMPLE}" 'NR <= n' \
        | cut -f3 > "${SAMPLE_FILE}"
    SAMPLED="$(wc -l < "${SAMPLE_FILE}" | tr -d ' ')"
    [[ "${SAMPLED}" -gt 0 ]] || { echo "No tasks under ${TASKS_DIR}" >&2; exit 1; }
    TASK_ARGS+=(--task-file "${SAMPLE_FILE}")
fi

echo "Tasks:       ${TASKS_DIR}"
echo "Ref:         $(git -C "${PRO_V2_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "Agent:       ${AGENT_ARG}"
echo "Model:       ${MODEL_ID:-${MODEL_NAME:-n/a}}"
echo "Pod:         ${OVERRIDE_CPUS} CPU / ${OVERRIDE_MEMORY_MB} MB  (task declares 1 / 4096)"
echo "Budget:      task 3000s x ${AGENT_TIMEOUT_MULTIPLIER:-1.0}  (exec ${ORCHARD_HARBOR_EXEC_TIMEOUT}s)"
echo "Network:     agent phase air-gapped by the task; verifier keeps egress"
echo "Concurrency: ${CONCURRENCY}  (attempts per task: ${ATTEMPTS})"
if [[ -n "${TASK_IDS:-}" ]]; then
    echo "Subset:      ${WANTED} named tasks (${TASK_IDS_FILE})"
elif [[ "${HARD51:-0}" == "1" ]]; then
    echo "Subset:      HARD-51"
else
    echo "Subset:      all 642"
fi
[[ -n "${SAMPLE_FILE}" ]] && echo "Sample:      ${SAMPLED} tasks, spread across repos (${SAMPLE_FILE})"
[[ -n "${REGRADE:-}" ]] && echo "Replaying:   ${REGRADE}"
echo "Job:         ${JOBS_DIR}/${JOB_NAME}"
echo

# Split the caller's trailing arguments at the first bare `--`, so the pod
# overrides can be spliced into the `harbor run` side rather than appended after
# a second separator — argparse strips only the first.
EVAL_ARGS=()
HARBOR_ARGS=(
    --override-cpus "${OVERRIDE_CPUS}"
    --override-memory-mb "${OVERRIDE_MEMORY_MB}"
    --agent-timeout-multiplier "${AGENT_TIMEOUT_MULTIPLIER:-1.0}"
    ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"}
)
separated=0
for arg in "${@:3}"; do
    if [[ ${separated} -eq 0 && "${arg}" == "--" ]]; then
        separated=1
    elif [[ ${separated} -eq 1 ]]; then
        HARBOR_ARGS+=("${arg}")
    else
        EVAL_ARGS+=("${arg}")
    fi
done

exec orchard-eval harbor \
    --path "${TASKS_DIR}" \
    --agent "${AGENT_ARG}" \
    ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} \
    --concurrency "${CONCURRENCY}" \
    --attempts "${ATTEMPTS}" \
    --jobs-dir "${JOBS_DIR}" \
    --job-name "${JOB_NAME}" \
    ${TASK_ARGS[@]+"${TASK_ARGS[@]}"} \
    ${EVAL_ARGS[@]+"${EVAL_ARGS[@]}"} \
    -- "${HARBOR_ARGS[@]}"
