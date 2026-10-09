#!/usr/bin/env bash
# Every harness against every benchmark, for one served model.
#
#   benchmarks: SWE-bench Verified, SWE-bench Multilingual, SWE-bench Pro,
#               SWE-bench Pro via Harbor, SWE-bench Pro V2, Terminal-Bench 2.1,
#               DeepSWE 1.1
#   harnesses:  mini-swe-agent, pi, codex   (in that order, within each)
#
# This file is a flat list of 18 commands, grouped by benchmark. Run it whole:
#
#     cd orchard_eval
#     ./scripts/run_all_evals.sh <MODEL_TAG> <MODEL_NAME> [HARNESSES] [BENCHMARKS] [LIMIT] [SAVE_ROOT]
#
#     # 2 instances/tasks per benchmark first — exercises every selected
#     # command, pod creation, every harness, grading and reporting, for ~nothing.
#     ./scripts/run_all_evals.sh qwen3.5-35b-iter42 /data/.../iter_0000042_hf all all 2
#
#     # then the real thing
#     ./scripts/run_all_evals.sh qwen3.5-35b-iter42 /data/.../iter_0000042_hf
#
#     # only some harnesses, or only some benchmarks (comma- or space-separated)
#     ./scripts/run_all_evals.sh qwen3.5-35b-iter42 /data/.../iter_0000042_hf pi,codex
#     ./scripts/run_all_evals.sh qwen3.5-35b-iter42 /data/.../iter_0000042_hf all deepswe1.1
#     HARNESSES=pi BENCHMARKS=tb2.1,deepswe1.1 ./scripts/run_all_evals.sh qwen3.5-35b-iter42 /data/.../iter_0000042_hf
#
# `all` is the five primary benchmarks. It leaves out two, both opt-in by name:
#
#   swebench-pro-harbor  measures SWE-bench Pro a *second* time through Harbor,
#                        another 731 long-horizon tasks per harness.
#   swebench-pro-v2      is a different benchmark, not a third path to that one:
#                        642 tasks with rewritten requirements, an air-gapped
#                        agent phase, and a git history sanitised of the fixing
#                        commit that V1 leaks. Each harness runs twice — once to
#                        solve, once to re-grade its patch in a fresh sandbox.
#                        Fetch the task tree first, it is not on the Hub:
#                            ./scripts/fetch_swebench_pro_v2.sh
#
# Ask for them by name, or say `everything` for all seven:
#
#     ./scripts/run_all_evals.sh <TAG> <MODEL> all swebench-pro,swebench-pro-harbor
#     ./scripts/run_all_evals.sh <TAG> <MODEL> all swebench-pro-v2
#     ./scripts/run_all_evals.sh <TAG> <MODEL> all everything
#
# PRO_V2_HARD51=1 narrows swebench-pro-v2 to upstream's HARD-51 subset — the 51
# tasks at least two of five frontier families failed — in both the agent and
# the re-grade passes. It writes to <harness>-swebench-pro-v2-hard51 rather than
# <harness>-swebench-pro-v2, so it never lands on top of a full V2 run:
#
#     PRO_V2_HARD51=1 ./scripts/run_all_evals.sh <TAG> <MODEL> all swebench-pro-v2
#
# For harnesses, `all` is mini-swe-agent, pi and codex. claude-code is opt-in for
# the same reason: a fourth harness across seven benchmarks is 8 more stages and
# the GPU time to match. Ask for it by name, or say `everything`:
#
#     ./scripts/run_all_evals.sh <TAG> <MODEL> claude-code
#     ./scripts/run_all_evals.sh <TAG> <MODEL> everything everything
#
# Then pair the two up per instance:
#
#     python scripts/compare_swebench_pro.py \
#         results/<TAG>/mini-swe-agent-swebench-pro \
#         results/<TAG>/harbor/mini-swe-agent-swebench-pro-harbor/<RUN_ID>
#
# A LIMIT run writes to <name>-smoke<N> instead of <name>, so a smoke test
# never lands in the directory a full run will use. Each Harbor job also gets a
# $RUN_ID subdirectory and each log file a $RUN_ID suffix, so re-running the same
# config leaves the previous attempt intact instead of writing over it — the same
# <run-name>/<run-id>/ shape `orchard-eval run` already uses. The scoreboard
# scores the newest attempt; `ls` shows them all.
#
# ...or export MODEL_TAG and MODEL_NAME, paste the rest of section 0 into a
# shell, and run any single command below on its own — each one is
# self-contained apart from the variables set there. Comment out the sections
# you do not want rather than editing the commands.
#
# Everything one model produces lands under a single prefix, so evaluating the
# next model is one argument and the two stay trivially comparable:
#
#     results/$MODEL_TAG/<harness>-<benchmark>/$RUN_ID/summary.json         # SWE-bench
#     results/$MODEL_TAG/harbor/<harness>-<benchmark>/$RUN_ID/result.json   # Harbor
#     logs/$MODEL_TAG/<harness>-<benchmark>-$RUN_ID.log
#
# Those are relative to orchard_eval/. SAVE_ROOT prefixes both trees, so a long
# run can land on a scratch volume instead of inside the checkout. It is the
# sixth argument or the environment variable of the same name; the variable is
# usually easier, since reaching the sixth slot means passing an empty LIMIT:
#
#     SAVE_ROOT=/data/eval-runs ./scripts/run_all_evals.sh qwen3.5-35b-iter42 /data/.../iter_0000042_hf
#     ./scripts/run_all_evals.sh qwen3.5-35b-iter42 /data/.../iter_0000042_hf all all "" /data/eval-runs
#
# ...which writes /data/eval-runs/results/$MODEL_TAG/... and
# /data/eval-runs/logs/$MODEL_TAG/... instead. Unset, nothing changes.
#
# Two benchmarks can be measured with or without the network, and both default
# to whatever the task itself declares:
#
#     DEEPSWE_ALLOW_INTERNET=1   DeepSWE, which declares no-network on all 113
#                                tasks, runs with full egress instead. Not
#                                comparable with an air-gapped score.
#     PRO_ALLOW_INTERNET=0       SWE-bench Pro, which declares allow_internet on
#                                all 731, runs air-gapped instead. Note 9 tasks
#                                `npm install` during verification and will fail.
#
# Either way the pinned model endpoint stays reachable, so an in-pod agent keeps
# working; everything else is denied. Neither has been exercised end to end
# against a live cluster, so smoke-test with a LIMIT of 2 before a full sweep.
#
# The whole matrix takes a long time, so the scoreboard is readable from another
# shell while it runs. Jobs still in flight are scored over what has finished so
# far and marked with a *:
#
#     python scripts/results_table.py results/<MODEL_TAG> --counts
#     watch -n 120 python scripts/results_table.py results/<MODEL_TAG> --counts
#
# Bracket the harness before spending anything on this. These are cheap, and
# each one caps every number below:
#
#     python scripts/check_model_from_sandbox.py
#     python scripts/gold_patch_check.py --limit 50    # expect ~100%
#     python scripts/no_patch_check.py   --limit 50    # expect ~0%
#     ./scripts/harbor_oracle_check.sh                 # expect ~99% on TB 2.1
#     ./scripts/deep_swe_oracle_check.sh 16 --limit 5
#
# For swebench-pro-v2, whose grader arrives as files rather than as a dataset,
# the equivalents are the ceiling and floor upstream gates its own release on
# (642/642 and 0/642). `SAMPLE=` rather than `--limit`: the latter is Harbor's
# n_tasks, which takes the first N of a sorted list — all NodeBB — while SAMPLE
# spreads the same N across the 11 repos:
#
#     ./scripts/fetch_swebench_pro_v2.sh
#     harbor-orchard audit third_party/SWE-bench_Pro-os/v2/tasks
#     SAMPLE=25 ./scripts/run_swebench_pro_v2.sh oracle 16 --threshold 1.0
#     SAMPLE=25 ./scripts/run_swebench_pro_v2.sh nop    16 --max-solve-rate 0.0

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1


# ===========================================================================
# 0. Setup
#
#   $1  MODEL_TAG   names this model's entire result set; nothing below can
#                   overwrite a previous model's numbers
#   $2  MODEL_NAME  the id the server actually serves, with no openai/ prefix
#   $3  HARNESSES   optional; which of mini-swe-agent, pi, codex and claude-code
#                   to run, comma- or space-separated, or `all` (the default).
#                   `all` is the first three; `everything` adds claude-code.
#                   Also settable as the $HARNESSES environment variable.
#   $4  BENCHMARKS  optional; which of swebench-verified, swebench-multilingual,
#                   swebench-pro, swebench-pro-harbor, swebench-pro-v2, tb2.1
#                   and deepswe1.1 to run, in the same form. `all` is every one
#                   but swebench-pro-harbor and swebench-pro-v2; `everything`
#                   includes them. Also settable as $BENCHMARKS.
#   $5  LIMIT       optional; run only N instances/tasks per benchmark
#   $6  SAVE_ROOT   optional; parent directory for both results/ and logs/.
#                   Defaults to empty, leaving them relative to orchard_eval/.
#                   Also settable as $SAVE_ROOT.
# ===========================================================================

export MODEL_TAG="${1:?usage: run_all_evals.sh <MODEL_TAG> <MODEL_NAME> [HARNESSES] [BENCHMARKS] [LIMIT] [SAVE_ROOT]}"
export MODEL_NAME="${2:?usage: run_all_evals.sh <MODEL_TAG> <MODEL_NAME> [HARNESSES] [BENCHMARKS] [LIMIT] [SAVE_ROOT]}"
export HARNESSES="${3:-${HARNESSES:-all}}"
export BENCHMARKS="${4:-${BENCHMARKS:-all}}"
export LIMIT="${5:-}"
export SAVE_ROOT="${6:-${SAVE_ROOT:-}}"

# A trailing slash is easy to type and would give "dir//results"; "/" is the one
# value that has nothing left to strip.
[ "$SAVE_ROOT" != "/" ] && SAVE_ROOT="${SAVE_ROOT%/}"

# Also the row and column order of everything below.
ALL_HARNESSES="mini-swe-agent pi codex claude-code"

# What `all` means for harnesses, mirroring DEFAULT_BENCHMARKS below.
# claude-code is opt-in by name: it is a fourth harness across six benchmarks,
# so folding it into the default would raise a bare run from 18 stages to 24 and
# the GPU bill with it. Ask for it explicitly, or say `everything`.
DEFAULT_HARNESSES="mini-swe-agent pi codex"
# Multiplier applied to each Harbor task's own `[agent] timeout_sec`. Floats are
# accepted. 1.5 because the base budgets assume frontier-speed serving and a
# self-hosted 27B is nowhere near it: on DeepSWE 1.1, 109 of 113 mini-swe-agent
# rollouts and 34 of 113 pi ones spent the whole 5400s without finishing, and
# every mini rollout that *did* finish on its own solved its task.
#
# Terminal-Bench keeps its own, higher multiplier: its tasks declare 900s, the
# shortest budget of the three, and it has been run at 2 since before this knob
# existed. Lowering it would be an unrelated change to a benchmark that is not
# timing out.
#
# These only move Harbor's deadline. ORCHARD_HARBOR_EXEC_TIMEOUT at each stage
# has to stay above the product, or the orchestrator stops waiting on the exec
# first and the rollout is lost rather than merely cut short:
#
#   swebench-pro-harbor  3000 x 1.5 = 4500   (exec 7200, in-pod 4380)
#   swebench-pro-v2      3000 x 1.0 = 3000   (exec 7200, in-pod 2880)
#   tb2.1                 900 x 2   = 1800   (exec 7500, in-pod 1680)
#   deepswe1.1           5400 x 1.5 = 8100   (exec 9000, in-pod 7980)
#
# swebench-pro-v2 is the exception to the multiplier, at 1.0. Upstream's locked
# protocol caps a task at 50 minutes and a longer budget makes the score
# incomparable with the figures it is meant to be read against — that is a
# property of the benchmark rather than of the fleet, so it does not follow
# $AGENT_TIMEOUT_MULTIPLIER. $PRO_V2_AGENT_TIMEOUT_MULTIPLIER overrides it.
#
# Every base is the task's own `[agent] timeout_sec`. Harbor's `--agent-timeout`
# would replace it, and the provider honours that too, but nothing here passes
# one: a multiplier scales all three benchmarks by the same argument about
# serving speed, where a per-benchmark absolute budget would have to be
# re-derived every time the model or the hardware changes.
#
# The in-pod column is the provider's own deadline, ORCHARD_HARBOR_AGENT_MARGIN
# (120s) under Harbor's. Harbor's deadline cancels an await and leaves the CLI
# running in the pod — on one 731-trial Pro run that orphaned 34% of timed-out
# trials for a median of 65 minutes each, 174 GPU-hours, and aged 189 pods past
# the sandbox TTL before they could be graded. Nothing to set here: both the
# multiplier and the override are exported to the provider automatically, so
# they cannot disagree with what Harbor is enforcing. See
# harbor_orchard/README.md, "Stopping an agent Harbor has stopped waiting for".
#
# All four numbers have to stay under SANDBOX_TTL_HOURS, which is 4h (14400s)
# as of 2026-09-20 — it was 2h, which the deepswe row alone exceeded.
#
# None of the above touches the native stages, whose budget is `harness.timeout`
# in the config, with `timeout_margin` under it for the in-pod deadline:
#
#   mini-swe-agent / pi / codex   1800  (in-pod 1740)
#   claude-code                   3600  (in-pod 3540)
#
# claude-code is deliberately given twice the budget, so a claude-code score and
# a pi score on the same benchmark are not directly comparable — part of any gap
# is the extra hour. Set `harness.timeout: 1800` in configs/claude-code.yaml to
# take that variable out, at the cost of more timed-out rollouts.
AGENT_TIMEOUT_MULTIPLIER="${AGENT_TIMEOUT_MULTIPLIER:-1.5}"
TB21_AGENT_TIMEOUT_MULTIPLIER="${TB21_AGENT_TIMEOUT_MULTIPLIER:-2}"

# Harbor's codex agent hard-defaults `reasoning_effort` to "high" and stages it
# as `-c model_reasoning_effort=high`. Qwen3.8-27B's chat template accepts
# xhigh, medium and low and raises on anything else, so left alone this 4xxs
# every request of every codex Harbor stage — the native arm is unaffected
# because it sends no effort at all. `null` parses to Python None, which drops
# the flag, which puts these stages on the template's own default('xhigh') and
# so on the same setting the native arm already measures. `none` does not work:
# it stays the string "none" and is staged verbatim.
CODEX_EFFORT_AK="reasoning_effort=null"


ALL_BENCHMARKS="swebench-verified swebench-multilingual swebench-pro swebench-pro-harbor swebench-pro-v2 tb2.1 deepswe1.1"

# What `all` means. swebench-pro-harbor is the same 731 tasks as swebench-pro,
# measured through a second, independent path; running it is a decision about
# trusting the numbers rather than about covering the matrix, so it is opt-in by
# name.
#
# swebench-pro-v2 is opt-in for a different reason: it is a *different*
# benchmark, not a second path to this one. 642 tasks against 731, rewritten
# requirements and interfaces, and a sanitised git history that removes the
# leak the V1 numbers were measured through. Nothing it reports is comparable
# with a swebench-pro figure, so folding it into `all` would put two
# incomparable columns side by side in the scoreboard. `everything` is the whole
# of $ALL_BENCHMARKS.
DEFAULT_BENCHMARKS="swebench-verified swebench-multilingual swebench-pro tb2.1 deepswe1.1"

[ "$HARNESSES" = all ] && HARNESSES="$DEFAULT_HARNESSES"
[ "$HARNESSES" = everything ] && HARNESSES="$ALL_HARNESSES"
[ "$BENCHMARKS" = all ] && BENCHMARKS="$DEFAULT_BENCHMARKS"
[ "$BENCHMARKS" = everything ] && BENCHMARKS="$ALL_BENCHMARKS"
export HARNESSES="${HARNESSES//,/ }"
export BENCHMARKS="${BENCHMARKS//,/ }"

for h in $HARNESSES; do
    case " $ALL_HARNESSES " in
        *" $h "*) ;;
        *) echo "unknown harness '$h' (expected one of: $ALL_HARNESSES)" >&2
           echo "\`all\` is '$DEFAULT_HARNESSES'; \`everything\` is all four." >&2
           exit 1 ;;
    esac
done

for b in $BENCHMARKS; do
    case " $ALL_BENCHMARKS " in
        *" $b "*) ;;
        *) echo "unknown benchmark '$b' (expected one of: $ALL_BENCHMARKS)" >&2
           echo "LIMIT is the fifth argument, after BENCHMARKS." >&2
           exit 1 ;;
    esac
done

# Endpoints are properties of whichever cluster and tunnel you are using, so
# they come from the environment rather than being baked in here:
#   MODEL_BASE_URL=http://<frps-public-ip>:30021/v1 \
#   SANDBOX_BASE_URL=http://<orchestrator-host> SANDBOX_API_KEY=<key> \
#       ./scripts/run_all_evals.sh ...
for _var in MODEL_BASE_URL SANDBOX_BASE_URL SANDBOX_API_KEY; do
    if [ -z "${!_var:-}" ]; then
        echo "$_var is not set; export it before running this script." >&2
        exit 1
    fi
done
unset _var
export MODEL_BASE_URL SANDBOX_BASE_URL SANDBOX_API_KEY
export API_KEY="${API_KEY:-token-abc123}"

# One scripts/session_router.py in front of the 8 sglang engines, so the GPU
# host publishes one frp tunnel instead of eight. Each rollout names itself in
# the URL path and the router keeps its whole agent loop on one engine, placing
# new sessions wherever there is the least work.
#
# The old layout — eight tunnels on ports 30021..30028, pinned by hashing the
# instance id — is still supported and one override away:
#   REPLICAS=8 MODEL_ROUTING=sticky ./scripts/run_all_evals.sh ...
export REPLICAS="${REPLICAS:-1}"
export MODEL_ROUTING="${MODEL_ROUTING:-session}"
export CONCURRENCY="${CONCURRENCY:-48}"

# MODEL_BASE_URL above is written for the *sandboxes*, so it names the frp
# tunnel. At the end of each trial the orchestrator makes one more call to the
# router on its own behalf — DELETE /router/session/<id>, which is what stops
# a finished rollout from counting as load — and that call is made from here,
# not from a pod. The two addresses are only the same when the tunnel answers
# in both directions, and on this cluster it does not: on 2026-09-21 the
# published port was unreachable from the driver pod while 127.0.0.1:8100
# answered in under a millisecond, so all 83 closes timed out, `sessions.closed`
# stayed at 0, and every session was reaped by the idle window instead — the
# precise bias the close was added to remove.
#
# So ask the local port whether it is there. Something answering /router/healthz
# on this host *is* the router; nothing answering leaves the close on
# MODEL_BASE_URL's host, exactly as before. Set ROUTER_CONTROL_URL to skip the
# probe, or to `-` to disable the override entirely.
export ROUTER_CONTROL_URL="${ROUTER_CONTROL_URL:-}"
if [ -z "$ROUTER_CONTROL_URL" ] && [ "$MODEL_ROUTING" = session ]; then
    _local_router="http://127.0.0.1:${ROUTER_LOCAL_PORT:-8100}"
    if curl -sf -m 2 -o /dev/null "$_local_router/router/healthz" 2>/dev/null; then
        ROUTER_CONTROL_URL="$_local_router"
        echo "session router answers locally; closing sessions at $ROUTER_CONTROL_URL"
    fi
    unset _local_router
fi
[ "$ROUTER_CONTROL_URL" = "-" ] && ROUTER_CONTROL_URL=""
export ORCHARD_ROUTER_CONTROL_URL="$ROUTER_CONTROL_URL"

# ${SAVE_ROOT:+...} expands to nothing when SAVE_ROOT is unset, so the default
# paths stay exactly "results/$MODEL_TAG" and "logs/$MODEL_TAG" — no "./" creeps
# into the run names, the banners or the scoreboard.
export OUT="${SAVE_ROOT:+$SAVE_ROOT/}results/$MODEL_TAG"
export LOGS="${SAVE_ROOT:+$SAVE_ROOT/}logs/$MODEL_TAG"

# One stamp for the whole sweep, so a re-run never lands on top of the last one.
# The native path already does this — `orchard-eval run` writes
# <run-name>/<run-id>/ — but the Harbor path names a job and nothing more, so
# re-running the same config points Harbor at a directory that already exists
# and `tee` truncates the previous log. Both get $RUN_ID appended.
#
# results_table.py is built for this: benchmark_label() matches the benchmark as
# a *substring* of the job directory name, and cells are filled from a sorted
# listing, so YYYYmmdd-HHMMSS sorts chronologically and the newest attempt wins
# the cell while the older ones stay on disk.
#
# Export it to re-open a specific attempt rather than starting a new one.
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"

# SAVE_ROOT is an arbitrary path now, so a typo or an unmounted volume is the
# likeliest way this run dies. Find out here rather than 15 commands later.
mkdir -p "$OUT" "$LOGS" || {
    echo "cannot create '$OUT' and '$LOGS'${SAVE_ROOT:+ (SAVE_ROOT=$SAVE_ROOT)}" >&2
    exit 1
}

# $LIMIT_ARG is spliced unquoted into every command, so it disappears entirely
# when empty. $SUFFIX keeps a smoke run out of the full run's directories.
if [ -n "$LIMIT" ]; then
    export LIMIT_ARG="--limit $LIMIT"
    export SUFFIX="-smoke$LIMIT"
else
    export LIMIT_ARG=""
    export SUFFIX=""
fi

# $TEMPERATURE is the sampling temperature mini-swe-agent sends with every
# request, in its native and its Harbor stages alike. Unset, it sends none and
# the server's default applies: under SGLang's default `--sampling-defaults
# model` that is the checkpoint's generation_config.json (Qwen3.8-27B: 1.0,
# with top_p 0.95 and top_k 20). The server's --preferred-sampling-params never
# reach /v1/chat/completions, so the client is the place to change it:
#
#   TEMPERATURE=0.9 ./scripts/run_all_evals.sh <TAG> <MODEL> mini-swe-agent tb2.1
#
# pi, codex and claude-code have no temperature setting here and ignore it.
# Spliced unquoted like $LIMIT_ARG, so both disappear when it is unset.
if [ -n "${TEMPERATURE:-}" ]; then
    if ! [[ "$TEMPERATURE" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        echo "TEMPERATURE must be a non-negative number; got '$TEMPERATURE'" >&2
        exit 1
    fi
    export MINI_RUN_TEMPERATURE="harness.params.temperature=$TEMPERATURE"
    export MINI_HARBOR_TEMPERATURE="--ak temperature=$TEMPERATURE"
    for h in $HARNESSES; do
        [ "$h" = mini-swe-agent ] || \
            echo "note: TEMPERATURE applies to mini-swe-agent only; $h keeps the server's default" >&2
    done
else
    export MINI_RUN_TEMPERATURE=""
    export MINI_HARBOR_TEMPERATURE=""
fi

banner() {
    echo
    echo "##########################################################################"
    echo "###  $*${LIMIT:+   (limit $LIMIT)}"
    echo "###  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "##########################################################################"
    echo
}

# Every command below is guarded by `stage <n> <harness> <benchmark> &&`, which
# prints the banner and returns non-zero when either list excludes that command.
stage() {
    local label
    label="$(benchmark_label "$3")"
    # An optional 4th argument distinguishes two stages that run the same
    # harness against the same benchmark — SWE-bench Pro V2 runs each agent
    # once and then re-grades it, and without this the banner and the skip
    # message are identical for both halves.
    [ -n "${4:-}" ] && label="$label — $4"
    case " $HARNESSES " in
        *" $2 "*) ;;
        *) echo "###  $1  $2 | $label   -- skipped ($2 not in HARNESSES)"; return 1 ;;
    esac
    case " $BENCHMARKS " in
        *" $3 "*) ;;
        *) echo "###  $1  $2 | $label   -- skipped ($3 not in BENCHMARKS)"; return 1 ;;
    esac
    banner "$1  $2 | $label"
}

benchmark_label() {
    case "$1" in
        swebench-verified)     echo "SWE-bench Verified" ;;
        swebench-multilingual) echo "SWE-bench Multilingual" ;;
        swebench-pro)          echo "SWE-bench Pro" ;;
        swebench-pro-harbor)   echo "SWE-bench Pro (Harbor)" ;;
        swebench-pro-v2)       echo "SWE-bench Pro V2${PRO_V2_HARD51_LABEL:-} (Harbor)" ;;
        tb2.1)                 echo "Terminal-Bench 2.1" ;;
        deepswe1.1)            echo "DeepSWE 1.1" ;;
        *)                     echo "$1" ;;
    esac
}

# Every command below pipes into `tee`, which makes stdout a pipe rather than a
# terminal. Python then block-buffers it, and anything that renders only on a
# TTY (Harbor's live progress display) falls back to plain lines.
# PYTHONUNBUFFERED fixes the buffering; only a real pty restores the rendering.
export PYTHONUNBUFFERED=1

# util-linux `script` is on every Linux box, so this needs nothing installed;
# `unbuffer` (apt install expect) is preferred because `script` also injects a
# CR at every line ending, which lands in the log file.
pty() {
    if command -v unbuffer >/dev/null 2>&1; then
        unbuffer "$@"
    elif script --version 2>/dev/null | grep -q util-linux; then
        # -e reports the child's exit code rather than script's own.
        script -qec "$(printf '%q ' "$@")" /dev/null
    else
        "$@"
    fi
}
export PTY=pty

# codex and pi read the endpoint from the config the suite stages in the pod, so
# a stray OPENAI_* in the shell only confuses them. mini-swe-agent gets its own
# copy inline, on the two commands that actually need it.
unset OPENAI_BASE_URL OPENAI_API_BASE OPENAI_API_KEY MSWEA_API_KEY

# Same argument for claude-code, and it bites harder: the operator's own shell is
# very often a Claude Code shell, and Harbor resolves its Anthropic credential
# straight from this environment. A leaked ANTHROPIC_API_KEY or
# CLAUDE_CODE_OAUTH_TOKEN would send a benchmark run to api.anthropic.com and
# bill it to a person — silently, because it would work.
unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL \
      CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR

banner "$MODEL_TAG  ->  $OUT   harnesses: $HARNESSES   benchmarks: $BENCHMARKS${TEMPERATURE:+   temperature: $TEMPERATURE}"


# ===========================================================================
# 1. SWE-bench Verified  (500 instances)
# ===========================================================================

stage "1/32" mini-swe-agent swebench-verified && \
$PTY orchard-eval run -c configs/mini-swe-agent.yaml \
  --output-dir "$OUT" --run-name "mini-swe-agent-swebench-verified$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  $MINI_RUN_TEMPERATURE \
  2>&1 | tee "$LOGS/mini-swe-agent-swebench-verified$SUFFIX-$RUN_ID.log"

stage "2/32" pi swebench-verified && \
$PTY orchard-eval run -c configs/pi.yaml \
  --output-dir "$OUT" --run-name "pi-swebench-verified$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/pi-swebench-verified$SUFFIX-$RUN_ID.log"

stage "3/32" codex swebench-verified && \
$PTY orchard-eval run -c configs/codex.yaml \
  --output-dir "$OUT" --run-name "codex-swebench-verified$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/codex-swebench-verified$SUFFIX-$RUN_ID.log"

# claude-code takes the same arguments as the three above, which is the point:
# the Anthropic Messages API, the bearer credential, and the /v1-less endpoint
# are all settled inside the harness, so nothing about the protocol difference
# reaches this file. Only reached when the harness is asked for by name — see
# DEFAULT_HARNESSES.
stage "4/32" claude-code swebench-verified && \
$PTY orchard-eval run -c configs/claude-code.yaml \
  --output-dir "$OUT" --run-name "claude-code-swebench-verified$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/claude-code-swebench-verified$SUFFIX-$RUN_ID.log"


# ===========================================================================
# 2. SWE-bench Multilingual
#
# The overlay comes second so its sandbox resources and timeouts win — its
# cargo/gradle/go builds need more than the defaults.
# ===========================================================================

stage "5/32" mini-swe-agent swebench-multilingual && \
$PTY orchard-eval run -c configs/mini-swe-agent.yaml -c configs/swe-bench-multilingual.yaml \
  --output-dir "$OUT" --run-name "mini-swe-agent-swebench-multilingual$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  $MINI_RUN_TEMPERATURE \
  2>&1 | tee "$LOGS/mini-swe-agent-swebench-multilingual$SUFFIX-$RUN_ID.log"

stage "6/32" pi swebench-multilingual && \
$PTY orchard-eval run -c configs/pi.yaml -c configs/swe-bench-multilingual.yaml \
  --output-dir "$OUT" --run-name "pi-swebench-multilingual$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/pi-swebench-multilingual$SUFFIX-$RUN_ID.log"

stage "7/32" codex swebench-multilingual && \
$PTY orchard-eval run -c configs/codex.yaml -c configs/swe-bench-multilingual.yaml \
  --output-dir "$OUT" --run-name "codex-swebench-multilingual$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/codex-swebench-multilingual$SUFFIX-$RUN_ID.log"

stage "8/32" claude-code swebench-multilingual && \
$PTY orchard-eval run -c configs/claude-code.yaml -c configs/swe-bench-multilingual.yaml \
  --output-dir "$OUT" --run-name "claude-code-swebench-multilingual$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/claude-code-swebench-multilingual$SUFFIX-$RUN_ID.log"


# ===========================================================================
# 3. SWE-bench Pro  (731 instances)
#
# Pro's repos are large and its builds are real (npm ci, go mod download), so
# these instances cost several times a Verified one.
# ===========================================================================

stage "9/32" mini-swe-agent swebench-pro && \
$PTY orchard-eval run -c configs/mini-swe-agent.yaml -c configs/swebench-pro.yaml \
  --output-dir "$OUT" --run-name "mini-swe-agent-swebench-pro$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  $MINI_RUN_TEMPERATURE \
  2>&1 | tee "$LOGS/mini-swe-agent-swebench-pro$SUFFIX-$RUN_ID.log"

stage "10/32" pi swebench-pro && \
$PTY orchard-eval run -c configs/pi.yaml -c configs/swebench-pro.yaml \
  --output-dir "$OUT" --run-name "pi-swebench-pro$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/pi-swebench-pro$SUFFIX-$RUN_ID.log"

stage "11/32" codex swebench-pro && \
$PTY orchard-eval run -c configs/codex.yaml -c configs/swebench-pro.yaml \
  --output-dir "$OUT" --run-name "codex-swebench-pro$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/codex-swebench-pro$SUFFIX-$RUN_ID.log"

# 88 of the 731 Pro images are musl, where the payload's glibc `claude` cannot
# execve. This arm runs the CLI directly rather than through Harbor's installed
# agent, so those instances fail on the binary rather than being reported with
# the reason harbor_orchard.agents raises — configs/swebench-pro-musl.txt lists
# them, and a claude-code Pro score should be read against that list.
stage "12/32" claude-code swebench-pro && \
$PTY orchard-eval run -c configs/claude-code.yaml -c configs/swebench-pro.yaml \
  --output-dir "$OUT" --run-name "claude-code-swebench-pro$SUFFIX" \
  --no-resume \
  --base-url-replicas "$REPLICAS" --routing "$MODEL_ROUTING" \
  --concurrency "$CONCURRENCY" \
  $LIMIT_ARG \
  2>&1 | tee "$LOGS/claude-code-swebench-pro$SUFFIX-$RUN_ID.log"


# ===========================================================================
# 3b. SWE-bench Pro, through Harbor  (731 tasks)
#
# The same 731 instances as section 3, measured through a path that shares
# nothing with it below the dataset: Harbor supplies the task, the instruction,
# the agent and the verifier, and this repository supplies only the pod. Two
# independent implementations agreeing is what makes either number believable.
#
# Not in `all` — ask for swebench-pro-harbor by name, or say `everything`.
#
# --override-cpus 4 --override-memory-mb 16384 restores parity with
# configs/swebench-pro.yaml; the tasks themselves declare 1 CPU and 4096 MB,
# which is thinner than the other path and would make the comparison a
# measurement of the pod.
#
# All 731 tasks set allow_internet = true and declare no network_mode, so by
# default the pod has full egress and there is no air-gap to honour. That is
# the dataset's choice, not a safe one: the agent can reach the upstream
# repository where the fix lives. Set PRO_ALLOW_INTERNET=0 to measure it
# air-gapped instead — ORCHARD_HARBOR_FORCE_ISOLATION overrides the task's
# declaration and isolates the pod, keeping only the pinned model endpoint
# reachable, the same allowlist DeepSWE runs on.
#
# PRO_ALLOW_INTERNET names what the *task* declares, so it defaults to 1 and the
# provider flag it drives is its inverse. Hence the derived variable: reading
# ORCHARD_HARBOR_FORCE_ISOLATION="${PRO_ALLOW_INTERNET}" directly would isolate
# the pod exactly when you asked for the network.
#
# Two things to know before comparing an air-gapped number with a networked one:
#   - They are not comparable with each other, or with the leaderboard.
#   - 9 of the 731 tasks `npm install` during verification and will fail
#     without egress. Add the registry to ORCHARD_HARBOR_EGRESS_ALLOW if you
#     need them.
# The network is not the only leak: `git reset --hard <base>` in every task's
# Dockerfile leaves the fix commit reachable in /app's history, which no
# network setting addresses.
#
# Pair the two up afterwards:
#   python scripts/compare_swebench_pro.py \
#       "$OUT/mini-swe-agent-swebench-pro" \
#       "$OUT/harbor/mini-swe-agent-swebench-pro-harbor"
#
# The dataset is pinned to @2 rather than @latest. `2` is a revision number —
# harbor reads a ref as a tag, a pure integer, or a sha256: digest, so the Hub's
# displayed "2.0.0" is not a usable ref and `@2.0.0` fails outright. Revision 2
# is what `latest` resolves to today, so this changes nothing about what runs;
# it stops a future revision 3 from changing it silently. Revisions 1 and 2 hold
# the same 731 task names with different content on every one.
#
# This is *not* SWE-bench Pro V2. V2 (2026-09-22, 642 tasks) is distributed as a
# task directory in scaleapi/SWE-bench_Pro-os, not on the Hub, so this path did
# not move when V2 landed. The other Pro path reads HuggingFace, where V2 *did*
# become the default config — configs/swebench-pro.yaml pins `revision: v1.0` to
# hold it still. Adopting V2 means moving both paths together and re-measuring
# the ceilings; compare_swebench_pro.py is pairing two benchmarks otherwise.
# ===========================================================================

# 0/false/no is the vocabulary the provider's own bool parsing accepts.
PRO_FORCE_ISOLATION=0
case "${PRO_ALLOW_INTERNET:-1}" in
    0|false|no|FALSE|NO) PRO_FORCE_ISOLATION=1 ;;
esac

stage "13/32" mini-swe-agent swebench-pro-harbor && \
MSWEA_API_KEY="$API_KEY" \
OPENAI_BASE_URL="$MODEL_BASE_URL" \
OPENAI_API_BASE="$MODEL_BASE_URL" \
OPENAI_API_KEY="$API_KEY" \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_FORCE_ISOLATION="${PRO_FORCE_ISOLATION:-0}" \
$PTY orchard-eval harbor -d scale-ai/swe-bench-pro@2 \
  --agent mini-swe-agent --model "openai/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/mini-swe-agent-swebench-pro-harbor$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus 4 --override-memory-mb 16384 \
     $MINI_HARBOR_TEMPERATURE \
  2>&1 | tee "$LOGS/mini-swe-agent-swebench-pro-harbor$SUFFIX-$RUN_ID.log"

stage "14/32" pi swebench-pro-harbor && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_FORCE_ISOLATION="${PRO_FORCE_ISOLATION:-0}" \
$PTY orchard-eval harbor -d scale-ai/swe-bench-pro@2 \
  --agent pi --model orchard/orchard-model -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/pi-swebench-pro-harbor$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus 4 --override-memory-mb 16384 \
  2>&1 | tee "$LOGS/pi-swebench-pro-harbor$SUFFIX-$RUN_ID.log"

stage "15/32" codex swebench-pro-harbor && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_FORCE_ISOLATION="${PRO_FORCE_ISOLATION:-0}" \
$PTY orchard-eval harbor -d scale-ai/swe-bench-pro@2 \
  --agent codex --model "openai/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/codex-swebench-pro-harbor$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER" \
     --ak "$CODEX_EFFORT_AK" \
     --override-cpus 4 --override-memory-mb 16384 \
  2>&1 | tee "$LOGS/codex-swebench-pro-harbor$SUFFIX-$RUN_ID.log"

# `anthropic/` is Harbor's provider prefix, not part of the served id; the
# endpoint the pod actually talks to is pinned by the environment either way.
# Harbor's ClaudeCode strips or keeps that prefix depending on whether it sees a
# configured base URL, and for a model id that is a filesystem path the two
# branches disagree — so on the first run of this stage, confirm the id in
# sglang's access log matches $MODEL_NAME exactly. If it does not, set
# ANTHROPIC_MODEL in harbor_orchard/environment.py::_endpoint_env, which merges
# last and therefore wins. pi sidesteps the question entirely by being invoked
# under a catalog alias.
stage "16/32" claude-code swebench-pro-harbor && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_FORCE_ISOLATION="${PRO_FORCE_ISOLATION:-0}" \
$PTY orchard-eval harbor -d scale-ai/swe-bench-pro@2 \
  --agent claude-code --model "anthropic/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/claude-code-swebench-pro-harbor$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus 4 --override-memory-mb 16384 \
  2>&1 | tee "$LOGS/claude-code-swebench-pro-harbor$SUFFIX-$RUN_ID.log"


# ===========================================================================
# 3b. SWE-bench Pro V2, via Harbor  (642 tasks, air-gapped, re-graded)
#
# Not in `all` — ask for swebench-pro-v2 by name, or say `everything`.
#
# This is a different benchmark from the two above, not a third path to them.
# It exists because V1 leaks its own answer: V1's images ship a clone whose
# history still contains the fixing commit, and the task name carries that
# commit's SHA, so `git show <sha> | git apply` is a complete solution. Audited
# over the 2026-09-21 sweep, 701 of 731 trials read that history and the 219
# that copied gold into a non-test source file scored 90% against 71% for the
# trials that never touched it. Call it 5 to 6 points of inflation, as a floor.
#
# V2 rebuilds every image from a sanitised bundle — no fixing commit, no stray
# refs, stashes or hooks — restores tests with `git apply
# /tests/test_patch.patch` rather than checking them out of the fix commit, and
# gates its release on the reference patch resolving 642/642 and an empty patch
# resolving none.
#
# Fetch the task tree first; it is not on the Hub, and it is the grader:
#
#   ./scripts/fetch_swebench_pro_v2.sh        # pins a commit, verifies SHA256SUMS
#   export PRO_V2_DIR=...                     # or leave the default below
#
# Four differences from the V1 stages, each of which produces a wrong number
# rather than an error if it is missed:
#
#   1. No ORCHARD_HARBOR_FORCE_ISOLATION. V1 needed it because all 731 tasks
#      declared allow_internet = true. V2 declares its own air-gap — `[agent]
#      network_mode = "no-network"` on every task — and deliberately leaves
#      `[verifier]` on the public baseline, because some Go tasks download
#      modules while testing. Forcing isolation here breaks grading.
#
#      That makes this the first dataset to ask the provider to *restore*
#      network after the agent phase (public -> no-network -> public). DeepSWE
#      stays isolated through its verifier, so that direction has never run.
#      The oracle check is what proves it: see the README.
#
#   2. The budget is the task's own 3000s (multiplier 1.0), not the house 1.5.
#      Upstream's locked protocol caps a task at 50 minutes.
#
#   3. Every agent stage is followed by a re-grade stage. The verifier still
#      runs in the agent's own container, so it still sees whatever the agent
#      left there; the replay pass applies each trial's captured model.patch to
#      a clean image and grades that instead. The re-graded figure is the one
#      to report, and upstream asks for both to be published.
#
#      model.patch comes from harbor_orchard.agents._ModelPatchCapture, which
#      every agent this repo publishes inherits. A replay that scores 0.00%
#      across the board means the capture did not happen — check for
#      "model.patch bytes:" in the trial logs, and that
#      ORCHARD_HARBOR_STOCK_AGENTS was not set on the agent pass.
#
#      A replay that scores *part* of the set zero usually means it ran before
#      the agent pass finished. A trial whose patch is missing is not skipped;
#      it applies nothing and takes a zero that reads exactly like a patch that
#      failed. pro_v2_source_ready below refuses to start a replay in that
#      state, and agent/replay.json records "patch": null wherever it happened.
#
#   4. The pod is still overridden to 4 CPU / 16 GiB against the declared
#      1 / 4096, as the V1 stages are. 1 CPU on element-web or teleport turns a
#      build into a timeout and a timeout reads as a model failure. This is a
#      deviation from upstream's own runs; PRO_V2_CPUS/PRO_V2_MEMORY_MB restore
#      parity if you want to compare against the published leaderboard.
#
# Nothing here is comparable with a swebench-pro number: different task count,
# rewritten requirements and interfaces, different fail-to-pass sets, and one
# of the two was measured through a leak.
# ===========================================================================

PRO_V2_DIR="${PRO_V2_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/third_party/SWE-bench_Pro-os}"
PRO_V2_TASKS="$PRO_V2_DIR/v2/tasks"
PRO_V2_AGENT_TIMEOUT_MULTIPLIER="${PRO_V2_AGENT_TIMEOUT_MULTIPLIER:-1.0}"
PRO_V2_CPUS="${PRO_V2_CPUS:-4}"
PRO_V2_MEMORY_MB="${PRO_V2_MEMORY_MB:-16384}"
# Set PRO_V2_REGRADE=0 to run only the agent passes — useful while bringing the
# benchmark up, when the direct number is all you need and the replay would
# double the pod bill for nothing.
PRO_V2_REGRADE="${PRO_V2_REGRADE:-1}"
# A re-grade trial is a patch apply plus the verifier — no model calls, no agent
# loop — so it finishes far sooner than an agent trial and the model endpoint
# is not what limits it. Run it at 3x the agent concurrency by default; the
# sandbox cluster is the only bound. Override when the pods run out first.
PRO_V2_REGRADE_CONCURRENCY="${PRO_V2_REGRADE_CONCURRENCY:-$((CONCURRENCY * 3))}"
# Re-grade pods get half the agent pod's CPU and memory (never below 1 CPU /
# 4096 MB, the task's own declared minimum), so 3x the pods costs 1.5x the
# cluster rather than 3x. A verifier that times out on the smaller pod scores
# zero exactly like a wrong patch — raise these back to
# PRO_V2_CPUS/PRO_V2_MEMORY_MB if re-grade trails direct.
PRO_V2_REGRADE_CPUS="${PRO_V2_REGRADE_CPUS:-$(( PRO_V2_CPUS / 2 > 1 ? PRO_V2_CPUS / 2 : 1 ))}"
PRO_V2_REGRADE_MEMORY_MB="${PRO_V2_REGRADE_MEMORY_MB:-$(( PRO_V2_MEMORY_MB / 2 > 4096 ? PRO_V2_MEMORY_MB / 2 : 4096 ))}"

# Set PRO_V2_HARD51=1 to run only upstream's HARD-51 subset, the number to
# report while the full set no longer separates models. The subset goes to the
# re-grade as well as to the agent pass, and has to: the re-grade finds no
# patch for the other 591 tasks, and a task with no patch is not skipped — it
# scores zero, which would put HARD-51's solves over a denominator of 642.
#
# The subset is a directory of 51 symlinks (stage_task_subset.sh), not a
# --task-file: Harbor's name filter resolves every task path once per pattern,
# 642 x 51 lookups at ~22 ms each on /data, which is 11-12 minutes of setup on
# every pass. Trials resolve the links, so they grade exactly as before.
#
# The job directories become <harness>-swebench-pro-v2-hard51[-regrade], which
# results_table.py reads as columns of their own rather than as the full set's.
PRO_V2_HARD51="${PRO_V2_HARD51:-0}"
PRO_V2_HARD51_FILE="$PRO_V2_DIR/v2/hard51_ids.txt"
if [ "$PRO_V2_HARD51" = 1 ]; then
    PRO_V2_RUN="swebench-pro-v2-hard51"
    PRO_V2_HARD51_LABEL=" HARD-51"
else
    PRO_V2_RUN="swebench-pro-v2"
    PRO_V2_HARD51_LABEL=""
fi

# Fail early and loudly rather than eight stages deep. Only when V2 was asked
# for: this block is evaluated on every run.
case " $BENCHMARKS " in
    *" swebench-pro-v2 "*)
        if [ ! -d "$PRO_V2_TASKS" ]; then
            echo "swebench-pro-v2 was requested but there is no task tree at:" >&2
            echo "  $PRO_V2_TASKS" >&2
            echo "Fetch and verify it first:  ./scripts/fetch_swebench_pro_v2.sh" >&2
            exit 1
        fi
        if [ "$PRO_V2_HARD51" = 1 ]; then
            if [ ! -s "$PRO_V2_HARD51_FILE" ]; then
                echo "PRO_V2_HARD51=1 but there is no HARD-51 list at:" >&2
                echo "  $PRO_V2_HARD51_FILE" >&2
                exit 1
            fi
            # The leaf is named `tasks` like the full tree's, since Harbor
            # records that basename as the trials' dataset name.
            bash scripts/stage_task_subset.sh \
                "$PRO_V2_TASKS" "$PRO_V2_HARD51_FILE" "$PRO_V2_DIR/v2/hard51/tasks" || exit 1
            PRO_V2_TASKS="$PRO_V2_DIR/v2/hard51/tasks"
        fi
        ;;
esac

# `harbor run --ak source_job=<dir>` wants the attempt directory — the one
# holding instance_*/ — which is exactly jobs-dir/job-name.
pro_v2_job_dir() { echo "$OUT/harbor/$1-$PRO_V2_RUN$SUFFIX/$RUN_ID"; }

# Refuse to replay a job that is not finished. The stages below are sequential,
# so the agent pass has returned by the time its re-grade starts — but `stage`
# can skip one, a `--limit` can shorten one, and a failed agent pass does not
# stop the next statement from running. In all three cases the replay would
# still run, and a task it finds no patch for is not skipped: PatchReplayAgent
# applies nothing, the verifier runs against a pristine tree, and the trial
# takes a zero indistinguishable from a patch that was replayed and failed.
#
# The 2026-09-24 sweep is the measurement. A replay launched by hand two hours
# before the agent job finished wrote `{"patch": null}` into 110 of 642
# agent/replay.json files, all of them in the alphabetical tail the agent had
# not reached — qutebrowser 70 of 73, protonmail 22 of 55, tutao 3 of 4,
# navidrome 1 of 52, none in the other seven repositories. It reported 78.2%
# against the 94.4% the trials with a patch actually scored, which reads as a
# 16-point verifier-tampering finding and is an ordering mistake.
#
# result.json is what to count: it is the file PatchReplayAgent._find_patch
# matches `task_name` against, and harbor writes it when a trial ends while the
# directory itself appears when the trial starts.
pro_v2_source_ready() {
    local src="$1" label="$2" trials done_
    if [ ! -d "$src" ]; then
        echo "###  $label -- skipped: no source job at $src"
        return 1
    fi
    trials=$(find "$src" -mindepth 1 -maxdepth 1 -type d -name 'instance_*' | wc -l | tr -d ' ')
    done_=$(find "$src" -mindepth 2 -maxdepth 2 -path '*/instance_*/result.json' | wc -l | tr -d ' ')
    if [ "$trials" -eq 0 ]; then
        echo "###  $label -- skipped: $src holds no trials"
        return 1
    fi
    if [ "$trials" != "$done_" ]; then
        echo "###  $label -- skipped: source job unfinished ($done_ of $trials trials)"
        echo "###  Replaying now would score the rest zero for having no patch yet."
        echo "###  Re-run it alone once the agent pass completes:"
        echo "###    REGRADE=$src ./scripts/run_swebench_pro_v2.sh $3 $PRO_V2_REGRADE_CONCURRENCY"
        return 1
    fi
    return 0
}

stage "17/32" mini-swe-agent swebench-pro-v2 && \
MSWEA_API_KEY="$API_KEY" \
OPENAI_BASE_URL="$MODEL_BASE_URL" \
OPENAI_API_BASE="$MODEL_BASE_URL" \
OPENAI_API_KEY="$API_KEY" \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_CREATE_TIMEOUT=1800 \
$PTY orchard-eval harbor -p "$PRO_V2_TASKS" \
  --agent mini-swe-agent --model "openai/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/mini-swe-agent-$PRO_V2_RUN$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$PRO_V2_AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus "$PRO_V2_CPUS" --override-memory-mb "$PRO_V2_MEMORY_MB" \
     $MINI_HARBOR_TEMPERATURE \
  2>&1 | tee "$LOGS/mini-swe-agent-$PRO_V2_RUN$SUFFIX-$RUN_ID.log"

[ "$PRO_V2_REGRADE" = 1 ] && \
stage "18/32" mini-swe-agent swebench-pro-v2 re-grade && \
pro_v2_source_ready "$(pro_v2_job_dir mini-swe-agent)" "18/32  mini-swe-agent | SWE-bench Pro V2 re-grade" mini-swe-agent && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_CREATE_TIMEOUT=1800 \
ORCHARD_HARBOR_MODEL_EGRESS=0 \
PYTHONPATH="$PRO_V2_DIR/v2/tooling${PYTHONPATH:+:$PYTHONPATH}" \
$PTY orchard-eval harbor -p "$PRO_V2_TASKS" \
  --agent patch_replay:PatchReplayAgent --model replay -n "$PRO_V2_REGRADE_CONCURRENCY" \
  --jobs-dir "$OUT/harbor/mini-swe-agent-$PRO_V2_RUN-regrade$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --ak "source_job=$(pro_v2_job_dir mini-swe-agent)" \
     --override-cpus "$PRO_V2_REGRADE_CPUS" --override-memory-mb "$PRO_V2_REGRADE_MEMORY_MB" \
  2>&1 | tee "$LOGS/mini-swe-agent-$PRO_V2_RUN-regrade$SUFFIX-$RUN_ID.log"

stage "19/32" pi swebench-pro-v2 && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_CREATE_TIMEOUT=1800 \
$PTY orchard-eval harbor -p "$PRO_V2_TASKS" \
  --agent pi --model orchard/orchard-model -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/pi-$PRO_V2_RUN$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$PRO_V2_AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus "$PRO_V2_CPUS" --override-memory-mb "$PRO_V2_MEMORY_MB" \
  2>&1 | tee "$LOGS/pi-$PRO_V2_RUN$SUFFIX-$RUN_ID.log"

[ "$PRO_V2_REGRADE" = 1 ] && \
stage "20/32" pi swebench-pro-v2 re-grade && \
pro_v2_source_ready "$(pro_v2_job_dir pi)" "20/32  pi | SWE-bench Pro V2 re-grade" pi && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_CREATE_TIMEOUT=1800 \
ORCHARD_HARBOR_MODEL_EGRESS=0 \
PYTHONPATH="$PRO_V2_DIR/v2/tooling${PYTHONPATH:+:$PYTHONPATH}" \
$PTY orchard-eval harbor -p "$PRO_V2_TASKS" \
  --agent patch_replay:PatchReplayAgent --model replay -n "$PRO_V2_REGRADE_CONCURRENCY" \
  --jobs-dir "$OUT/harbor/pi-$PRO_V2_RUN-regrade$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --ak "source_job=$(pro_v2_job_dir pi)" \
     --override-cpus "$PRO_V2_REGRADE_CPUS" --override-memory-mb "$PRO_V2_REGRADE_MEMORY_MB" \
  2>&1 | tee "$LOGS/pi-$PRO_V2_RUN-regrade$SUFFIX-$RUN_ID.log"

stage "21/32" codex swebench-pro-v2 && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_CREATE_TIMEOUT=1800 \
$PTY orchard-eval harbor -p "$PRO_V2_TASKS" \
  --agent codex --model "openai/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/codex-$PRO_V2_RUN$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$PRO_V2_AGENT_TIMEOUT_MULTIPLIER" \
     --ak "$CODEX_EFFORT_AK" \
     --override-cpus "$PRO_V2_CPUS" --override-memory-mb "$PRO_V2_MEMORY_MB" \
  2>&1 | tee "$LOGS/codex-$PRO_V2_RUN$SUFFIX-$RUN_ID.log"

[ "$PRO_V2_REGRADE" = 1 ] && \
stage "22/32" codex swebench-pro-v2 re-grade && \
pro_v2_source_ready "$(pro_v2_job_dir codex)" "22/32  codex | SWE-bench Pro V2 re-grade" codex && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_CREATE_TIMEOUT=1800 \
ORCHARD_HARBOR_MODEL_EGRESS=0 \
PYTHONPATH="$PRO_V2_DIR/v2/tooling${PYTHONPATH:+:$PYTHONPATH}" \
$PTY orchard-eval harbor -p "$PRO_V2_TASKS" \
  --agent patch_replay:PatchReplayAgent --model replay -n "$PRO_V2_REGRADE_CONCURRENCY" \
  --jobs-dir "$OUT/harbor/codex-$PRO_V2_RUN-regrade$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --ak "source_job=$(pro_v2_job_dir codex)" \
     --override-cpus "$PRO_V2_REGRADE_CPUS" --override-memory-mb "$PRO_V2_REGRADE_MEMORY_MB" \
  2>&1 | tee "$LOGS/codex-$PRO_V2_RUN-regrade$SUFFIX-$RUN_ID.log"

stage "23/32" claude-code swebench-pro-v2 && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_CREATE_TIMEOUT=1800 \
$PTY orchard-eval harbor -p "$PRO_V2_TASKS" \
  --agent claude-code --model "anthropic/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/claude-code-$PRO_V2_RUN$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$PRO_V2_AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus "$PRO_V2_CPUS" --override-memory-mb "$PRO_V2_MEMORY_MB" \
  2>&1 | tee "$LOGS/claude-code-$PRO_V2_RUN$SUFFIX-$RUN_ID.log"

[ "$PRO_V2_REGRADE" = 1 ] && \
stage "24/32" claude-code swebench-pro-v2 re-grade && \
pro_v2_source_ready "$(pro_v2_job_dir claude-code)" "24/32  claude-code | SWE-bench Pro V2 re-grade" claude-code && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7200 \
ORCHARD_HARBOR_CREATE_TIMEOUT=1800 \
ORCHARD_HARBOR_MODEL_EGRESS=0 \
PYTHONPATH="$PRO_V2_DIR/v2/tooling${PYTHONPATH:+:$PYTHONPATH}" \
$PTY orchard-eval harbor -p "$PRO_V2_TASKS" \
  --agent patch_replay:PatchReplayAgent --model replay -n "$PRO_V2_REGRADE_CONCURRENCY" \
  --jobs-dir "$OUT/harbor/claude-code-$PRO_V2_RUN-regrade$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --ak "source_job=$(pro_v2_job_dir claude-code)" \
     --override-cpus "$PRO_V2_REGRADE_CPUS" --override-memory-mb "$PRO_V2_REGRADE_MEMORY_MB" \
  2>&1 | tee "$LOGS/claude-code-$PRO_V2_RUN-regrade$SUFFIX-$RUN_ID.log"


# ===========================================================================
# 4. Terminal-Bench 2.1  (90 tasks)
#
# Harbor owns the trial; harbor_orchard owns only the environment. Arguments
# after the bare `--` go to `harbor run` itself. The tasks declare 2 CPUs and
# 8 GiB, which is thin for a build under an agent that compiles repeatedly.
# ===========================================================================

# Harbor's mini-swe-agent is a BaseInstalledAgent: it installs and runs the CLI
# *in the pod*, so --base-url-replicas pins each trial exactly as for pi and
# codex (OrchardEnvironment.exec applies the pinned endpoint last, overriding
# whatever the agent configured). MSWEA_API_KEY is read on this host and passed
# through into the container; mini refuses to start without it.
stage "25/32" mini-swe-agent tb2.1 && \
MSWEA_API_KEY="$API_KEY" \
OPENAI_BASE_URL="$MODEL_BASE_URL" \
OPENAI_API_BASE="$MODEL_BASE_URL" \
OPENAI_API_KEY="$API_KEY" \
ORCHARD_HARBOR_EXEC_TIMEOUT=7500 \
$PTY orchard-eval harbor -d terminal-bench/terminal-bench-2-1@latest \
  --agent mini-swe-agent --model "openai/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/mini-swe-agent-tb2.1$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$TB21_AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus 8 --override-memory-mb 32768 \
     $MINI_HARBOR_TEMPERATURE \
  2>&1 | tee "$LOGS/mini-swe-agent-tb2.1$SUFFIX-$RUN_ID.log"

# pi resolves the endpoint from its staged in-pod config, so the model string is
# a catalog alias rather than the served id.
stage "26/32" pi tb2.1 && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7500 \
$PTY orchard-eval harbor -d terminal-bench/terminal-bench-2-1@latest \
  --agent pi --model orchard/orchard-model -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/pi-tb2.1$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$TB21_AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus 8 --override-memory-mb 32768 \
  2>&1 | tee "$LOGS/pi-tb2.1$SUFFIX-$RUN_ID.log"

stage "27/32" codex tb2.1 && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7500 \
$PTY orchard-eval harbor -d terminal-bench/terminal-bench-2-1@latest \
  --agent codex --model "openai/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/codex-tb2.1$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$TB21_AGENT_TIMEOUT_MULTIPLIER" \
     --ak "$CODEX_EFFORT_AK" \
     --override-cpus 8 --override-memory-mb 32768 \
  2>&1 | tee "$LOGS/codex-tb2.1$SUFFIX-$RUN_ID.log"

# The cheapest of the six claude-code stages — 90 tasks, all glibc — which makes
# it the one to smoke-test the Harbor arm on.
stage "28/32" claude-code tb2.1 && \
ORCHARD_HARBOR_EXEC_TIMEOUT=7500 \
$PTY orchard-eval harbor -d terminal-bench/terminal-bench-2-1@latest \
  --agent claude-code --model "anthropic/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/claude-code-tb2.1$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --agent-timeout-multiplier "$TB21_AGENT_TIMEOUT_MULTIPLIER" \
     --override-cpus 8 --override-memory-mb 32768 \
  2>&1 | tee "$LOGS/claude-code-tb2.1$SUFFIX-$RUN_ID.log"


# ===========================================================================
# 5. DeepSWE 1.1  (113 tasks)
#
# The task budget is 10800s, but the orchestrator reaps a sandbox at
# SANDBOX_TTL_HOURS (2h), so an exec timeout past that only parks a trial on a
# pod that no longer exists. 7200 ends the agent loop at the same moment, which
# Harbor records as an ordinary non-zero exit. Raise both together.
#
# All 113 tasks declare network_mode = "no-network" on [agent] and [verifier],
# and that is honoured by default: the pod starts on the public [environment]
# baseline so the build can run, and Harbor switches it to isolated before
# agent.run(). Only the pinned model endpoint stays reachable, so the in-pod
# agent still gets inference and nothing else — which is the point. The images
# gc the repository's future history so the reference solution cannot leak from
# git, and egress would put it one `git clone` away.
#
# DEEPSWE_ALLOW_INTERNET=1 gives the pod full egress instead, ignoring the
# air-gap. For bringing the benchmark up when the allowlist is not right yet.
# A score measured that way is not comparable with an air-gapped one or with
# the leaderboard, and the provider says so once per run.
#
# The allowlist is derived from MODEL_BASE_URL — host and port, nothing else.
# Add anything further with ORCHARD_HARBOR_EGRESS_ALLOW. Verify isolation is
# real rather than merely broken with scripts/deep_swe_oracle_check.sh, which
# checks that the endpoint answers *and* that `git clone` of the upstream
# repository fails.
#
# pi runs here with a 32768-token cap rather than the 16384 everywhere else.
# pi ends the whole session on the first truncated completion instead of
# re-prompting, so the cap decides how many rollouts commit anything at all:
# on the 2026-09-21 sweep 84% of finished pi trials ended on
# `stopReason=length`, and those solved 6.9% of their tasks against 66.7% for
# the ones that ended cleanly. Raising it does not make truncation free — it
# converts truncated rollouts into timed-out ones, which on SWE-bench Verified
# was a wash (70.0% -> 68.8%, harbor_orchard/README.md). What differs here is
# the budget it is being spent against: Verified gives a rollout 1800s, so a
# 32768-token runaway `thinking` block at ~25 tok/s eats most of it, while
# DeepSWE gives 5400 x 1.5 = 8100s and the same block costs a fifth. The trade
# that did not pay there should pay here. DEEPSWE_PI_MAX_TOKENS overrides it;
# empty lets pi choose its own.
# ===========================================================================

stage "29/32" mini-swe-agent deepswe1.1 && \
MSWEA_API_KEY="$API_KEY" \
OPENAI_BASE_URL="$MODEL_BASE_URL" \
OPENAI_API_BASE="$MODEL_BASE_URL" \
OPENAI_API_KEY="$API_KEY" \
ORCHARD_HARBOR_ALLOW_INTERNET="${DEEPSWE_ALLOW_INTERNET:-0}" \
ORCHARD_HARBOR_EXEC_TIMEOUT=9000 \
$PTY orchard-eval harbor -d datacurve/deep-swe-1-1@latest \
  --agent mini-swe-agent --model "openai/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/mini-swe-agent-deepswe1.1$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --override-cpus 8 --override-memory-mb 32768 \
     --agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER" \
     $MINI_HARBOR_TEMPERATURE \
  2>&1 | tee "$LOGS/mini-swe-agent-deepswe1.1$SUFFIX-$RUN_ID.log"

stage "30/32" pi deepswe1.1 && \
ORCHARD_HARBOR_ALLOW_INTERNET="${DEEPSWE_ALLOW_INTERNET:-0}" \
ORCHARD_HARBOR_EXEC_TIMEOUT=9000 \
ORCHARD_HARBOR_PI_MAX_TOKENS="${DEEPSWE_PI_MAX_TOKENS-32768}" \
$PTY orchard-eval harbor -d datacurve/deep-swe-1-1@latest \
  --agent pi --model orchard/orchard-model -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/pi-deepswe1.1$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --override-cpus 8 --override-memory-mb 32768 \
     --agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER" \
  2>&1 | tee "$LOGS/pi-deepswe1.1$SUFFIX-$RUN_ID.log"

stage "31/32" codex deepswe1.1 && \
ORCHARD_HARBOR_ALLOW_INTERNET="${DEEPSWE_ALLOW_INTERNET:-0}" \
ORCHARD_HARBOR_EXEC_TIMEOUT=9000 \
$PTY orchard-eval harbor -d datacurve/deep-swe-1-1@latest \
  --agent codex --model "openai/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/codex-deepswe1.1$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --override-cpus 8 --override-memory-mb 32768 \
     --ak "$CODEX_EFFORT_AK" \
     --agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER" \
  2>&1 | tee "$LOGS/codex-deepswe1.1$SUFFIX-$RUN_ID.log"

# The air-gap is what makes the payload mandatory here rather than merely
# preferable: Harbor's own fallback for this agent fetches from npm or
# downloads.claude.ai, and on an isolated pod neither host resolves. A trial
# that reaches that fallback is one where the payload probe failed.
stage "32/32" claude-code deepswe1.1 && \
ORCHARD_HARBOR_ALLOW_INTERNET="${DEEPSWE_ALLOW_INTERNET:-0}" \
ORCHARD_HARBOR_EXEC_TIMEOUT=9000 \
$PTY orchard-eval harbor -d datacurve/deep-swe-1-1@latest \
  --agent claude-code --model "anthropic/$MODEL_NAME" -n "$CONCURRENCY" \
  --base-url "$MODEL_BASE_URL" --base-url-replicas "$REPLICAS" \
  --routing "$MODEL_ROUTING" \
  --jobs-dir "$OUT/harbor/claude-code-deepswe1.1$SUFFIX" --job-name "$RUN_ID" \
  $LIMIT_ARG \
  -- --override-cpus 8 --override-memory-mb 32768 \
     --agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER" \
  2>&1 | tee "$LOGS/claude-code-deepswe1.1$SUFFIX-$RUN_ID.log"


# ===========================================================================
# 6. The scoreboard
#
# Re-runnable on its own at any time, including while a run is still going — it
# only reads what is already on disk. --format markdown or csv gives something
# to paste into a doc or a sheet.
# ===========================================================================

banner "done  |  $MODEL_TAG"

python scripts/results_table.py "$OUT" --counts ${LIMIT:+--include-smoke}

echo "  SWE-bench detail:  orchard-eval report --results $OUT/<run-name>/<run-id>/results.jsonl"
echo "  Fleet spread:      jq -r '.metrics.base_url' $OUT/*/*/results.jsonl | sort | uniq -c"
echo "  Logs:              $LOGS/"

# Only meaningful when both SWE-bench Pro paths ran; the table above shows them
# as two columns, and this says which instances they disagreed on.
case " $BENCHMARKS " in
    *" swebench-pro "*)
        case " $BENCHMARKS " in
            *" swebench-pro-harbor "*)
                echo
                echo "  SWE-bench Pro was measured twice. Pair the two up per instance:"
                for h in $HARNESSES; do
                    echo "    python scripts/compare_swebench_pro.py \\"
                    echo "        $OUT/$h-swebench-pro$SUFFIX \\"
                    echo "        $OUT/harbor/$h-swebench-pro-harbor$SUFFIX"
                done
                ;;
        esac
        ;;
esac
