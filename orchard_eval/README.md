# Orchard Eval Suite

Run **any agent harness** — `codex`, `claude-code`, `pi`, `opencode`,
`mini-swe-agent` — against SWE-bench (Verified, Multilingual, Pro) and
Harbor-format benchmarks (Terminal-Bench 2.1, DeepSWE 1.1, SWE-bench Pro V2) on
[Orchard Env](../orchard_env/README.md) sandboxes.

Benchmark, environment and agent are separate layers, so swapping one never
means rewriting the others:

| Layer | Code (in `orchard_evalkit/`) | Swap it by |
| --- | --- | --- |
| **Benchmark** | `datasets/`, `grading/` | `dataset.name` |
| **Environment** | `sandbox.py`, `runner.py` | — (Orchard Env pods) |
| **Harness** | `harnesses/` | `harness.name` |

Every Orchard Env sandbox already ships `codex`, `claude`, `pi`, `opencode`,
`hermes` and `mini` on `PATH` in **any** base image
([details](../orchard_env/README.md#built-in-agent-harnesses)), so evaluating an
agent is a single `exec` into a pod that already holds the repository — no image
rebuilds. Harbor tasks run through [`harbor_orchard`](harbor_orchard/README.md),
a Harbor environment provider that turns a task's Dockerfile into a pod.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Benchmarks](#benchmarks)
- [Sanity checks](#sanity-checks)
- [Harnesses](#harnesses)
- [Configuration](#configuration)
- [Model serving and routing](#model-serving-and-routing)
- [Output and trajectories](#output-and-trajectories)
- [Grading](#grading)
- [Resume and failure handling](#resume-and-failure-handling)
- [Debugging a failed trial](#debugging-a-failed-trial)
- [Known caveats](#known-caveats)

## Install

Tested in the `mirror.gcr.io/lmsysorg/sglang:v0.5.18` image. From the
repository root:

```bash
python -m pip install -e orchard_env -e "orchard_eval[swebench]"
python -m pip install --ignore-installed PyJWT
python -m pip install harbor==0.22.0
python -m pip install -e orchard_eval/harbor_orchard
python -m pip install 'tokenizers==0.22.2' 'pytest-asyncio==1.4.0'

orchard-eval list-harnesses        # verify, no cluster needed
```

## Quick start

All commands run from `orchard_eval/`.

### 1. Serve the model

Any OpenAI-compatible server works. For sglang, run **one server per GPU** and
put the session router in front, so each rollout stays on one engine and keeps
its prefix cache warm (a single `--dp 8` server load-balances per *request* and
re-prefills the conversation every turn):

```bash
# 8 sglang servers on ports 8000..8007, one GPU each
MODEL_PATH=/path/to/model REPLICAS=8 ./scripts/serve_sglang_fleet.sh

# One router on :8100 in front of all eight
python scripts/session_router.py --replicas 8 --base-port 8000 --port 8100 \
    --api-key token-abc123
```

Extra sglang flags pass straight through, e.g.
`./scripts/serve_sglang_fleet.sh --preferred-sampling-params '{"temperature":1.0,"top_p":0.95}'`.
Qwen3.8-27B with EAGLE speculative decoding and an fp8 KV cache:

```bash
MODEL_PATH=/path/to/models/Qwen/Qwen3.8-27B SGLANG_ENABLE_SPEC_V2=1 REPLICAS=8 \
    ./scripts/serve_sglang_fleet.sh \
    --trust-remote-code --allow-auto-truncate \
    --kv-cache-dtype fp8_e4m3 --mem-fraction-static 0.85 \
    --chunked-prefill-size 32768 --max-prefill-tokens 32768 \
    --speculative-algorithm EAGLE --speculative-num-steps 3 \
    --speculative-eagle-topk 1 --speculative-num-draft-tokens 4 \
    --preferred-sampling-params '{"temperature":1.0,"top_p":0.95,"top_k":20,"min_p":0.0,"repetition_penalty":1.0}'
```

### 2. Expose it to the sandboxes (Optional)

Agents run **inside the sandbox pods**, so the model endpoint must be reachable
from there. If the GPU host is not directly reachable, publish it through an
[frp](https://github.com/fatedier/frp) server:

```bash
curl -L https://github.com/fatedier/frp/releases/download/v0.61.1/frp_0.61.1_linux_amd64.tar.gz \
    | tar -xz -C /tmp && cp /tmp/frp_0.61.1_linux_amd64/frpc /tmp/frpc

FRP_SERVER_ADDR=<frps-public-ip> FRP_TOKEN=<frps-token> \
REPLICAS=1 BASE_PORT=8100 REMOTE_BASE_PORT=30021 ./scripts/frpc_fleet.sh
```

`REPLICAS=1` publishes only the router's port; the router spreads the traffic
over the eight engines behind it.

### 3. Set credentials

```bash
export MODEL_NAME="/path/to/model"                        # id the server serves, no `openai/` prefix
export MODEL_BASE_URL="http://<frps-public-ip>:30021/v1"  # reachable from the pods
export API_KEY="token-abc123"                             # any placeholder if the server has no --api-key

export SANDBOX_BASE_URL="http://<orchestrator-host>"
export SANDBOX_API_KEY="<sandbox-api-key>"
```

Never commit real values for `SANDBOX_BASE_URL` / `SANDBOX_API_KEY`; the
scripts read them from the environment and refuse to start without them.

### 4. Check the endpoint

Probes this host → model, the orchestrator, and a real pod → model separately,
and reports which wire APIs (`/v1/responses`, `/v1/chat/completions`,
`/v1/messages`) work:

```bash
python scripts/check_model_from_sandbox.py --replicas 1 --sessions 4
```

### 5. Run the evaluation

`scripts/run_all_evals.sh` runs every selected harness against every selected
benchmark for one model, writing everything under one prefix:

```bash
./scripts/run_all_evals.sh <MODEL_TAG> <MODEL_NAME> [HARNESSES] [BENCHMARKS] [LIMIT] [SAVE_ROOT]

# Smoke test: 2 instances per benchmark, every harness
./scripts/run_all_evals.sh my-model "$MODEL_NAME" all all 2

# Full run of selected harnesses and benchmarks
CONCURRENCY=32 SAVE_ROOT=/path/to/eval_runs \
    ./scripts/run_all_evals.sh my-model "$MODEL_NAME" mini-swe-agent,pi \
    swebench-verified,swebench-multilingual,tb2.1

CONCURRENCY=32 DEEPSWE_ALLOW_INTERNET=0 PRO_ALLOW_INTERNET=0 SAVE_ROOT=/path/to/eval_runs \
    ./scripts/run_all_evals.sh my-model "$MODEL_NAME" mini-swe-agent,pi \
    swebench-pro-harbor,deepswe1.1

# Real example to evaluate Qwen3.8-27B on all benchmarks
CONCURRENCY=24 DEEPSWE_ALLOW_INTERNET=0 PRO_ALLOW_INTERNET=0 \
ORCHARD_HARBOR_COMMIT_BEFORE_COLLECT=1 \
SAVE_ROOT=/path/to/eval_runs \
    ./scripts/run_all_evals.sh Qwen3.8-27B \
    /path/to/models/Qwen/Qwen3.8-27B codex \
    swebench-verified,swebench-multilingual,swebench-pro-v2,tb2.1,deepswe1.1

# mini-swe-agent at temperature 0.9, SWE-bench Pro V2 narrowed to HARD-51
PRO_V2_HARD51=1 TEMPERATURE=0.9 CONCURRENCY=32 \
DEEPSWE_ALLOW_INTERNET=0 PRO_ALLOW_INTERNET=0 \
ORCHARD_HARBOR_COMMIT_BEFORE_COLLECT=1 \
SAVE_ROOT=/path/to/eval_runs \
    ./scripts/run_all_evals.sh Qwen3.8-27B \
    /path/to/models/Qwen/Qwen3.8-27B mini-swe-agent \
    swebench-pro-v2,tb2.1,deepswe1.1
```

| Argument | Values |
| --- | --- |
| `HARNESSES` | `mini-swe-agent`, `pi`, `codex`, `claude-code`; `all` = first three, `everything` = all four |
| `BENCHMARKS` | `swebench-verified`, `swebench-multilingual`, `swebench-pro`, `swebench-pro-harbor`, `swebench-pro-v2`, `tb2.1`, `deepswe1.1`; `all` = the five without `-harbor`/`-v2`, `everything` = all seven |
| `LIMIT` | N instances per benchmark; writes to `<name>-smoke<N>` |
| `SAVE_ROOT` | Prefix for `results/` and `logs/` (also an env var) |

Both lists are comma- or space-separated and can also be set as env vars.
`DEEPSWE_ALLOW_INTERNET=1` / `PRO_ALLOW_INTERNET=0` override the tasks' own
network declarations (scores are then not comparable with the leaderboard).
`PRO_V2_HARD51=1` runs only upstream's HARD-51 subset of `swebench-pro-v2`,
written to `<harness>-swebench-pro-v2-hard51` so it never overwrites a full V2
run. `TEMPERATURE=T` sets mini-swe-agent's sampling temperature; unset, the
server default applies.

### 6. Read the results

The scoreboard is readable while the matrix is still running (in-flight jobs are
marked `*`):

```bash
python scripts/results_table.py results/<MODEL_TAG> --counts
```

Two more tables follow: mean agent turns (LLM responses) and mean input / output
tokens per trial, each as `all (solved)`. A re-grade column reports the solving
run whose patch it replayed. Turns cost one trajectory read per Harbor trial;
`--no-efficiency` skips both tables.

For a single run:

```bash
RUN="results/<run_name>/$(ls -1 results/<run_name> | tail -1)"
cat "$RUN/summary.json"                              # resolve rate, exit statuses, cost
orchard-eval report --results "$RUN/results.jsonl"   # re-aggregate a partial run
```

## Benchmarks

| Benchmark | Path | Tasks | Single-harness command |
| --- | --- | --- | --- |
| SWE-bench Verified | native | 500 | `./scripts/run_swebench_verified.sh codex 64` |
| SWE-bench Multilingual | native | 300 | `orchard-eval run -c configs/codex.yaml -c configs/swe-bench-multilingual.yaml` |
| SWE-bench Pro (V1) | native | 731 | `./scripts/run_swebench_pro.sh mini-swe-agent 32` |
| SWE-bench Pro (V1) | Harbor | 731 | `./scripts/run_swebench_pro_harbor.sh mini-swe-agent 32` |
| SWE-bench Pro V2 | Harbor | 642 | `./scripts/run_swebench_pro_v2.sh mini-swe-agent 24` |
| Terminal-Bench 2.1 | Harbor | 90 | `./scripts/run_terminal_bench.sh codex 32` |
| DeepSWE 1.1 | Harbor | 113 | `./scripts/run_deep_swe.sh pi 32` |

**Native** runs use `orchard-eval run`: a fresh rollout pod for the agent and a
second fresh pod for grading. **Harbor** runs use `orchard-eval harbor`, which
drives `harbor run` with the Orchard environment provider and re-reads its
results — see [harbor_orchard/README.md](harbor_orchard/README.md) for provider
settings.

Anything after the concurrency in a wrapper script is passed through as a
dotted override (native) or Harbor flag, e.g.
`./scripts/run_swebench_verified.sh codex 64 dataset.limit=100`.

### SWE-bench (native)

```bash
orchard-eval run -c configs/codex.yaml --concurrency 64
orchard-eval run -c configs/mini-swe-agent.yaml -c configs/swebench-pro.yaml   # overlay goes second
orchard-eval run -c configs/codex.yaml --instance-id astropy__astropy-12907 --no-grading
```

Supported `dataset.name` values:

| `dataset.name` | HuggingFace dataset | Requires |
| --- | --- | --- |
| `swe-bench-verified` | `SWE-bench/SWE-bench_Verified` | swebench >= 5.0 |
| `swe-bench-verified-legacy` | `princeton-nlp/SWE-bench_Verified` | swebench < 5 |
| `swe-bench-lite` / `swe-bench-full` | `SWE-bench/SWE-bench_Lite` / `_full` | swebench >= 5.0 |
| `swe-bench-multilingual` | `SWE-bench/SWE-bench_Multilingual` | swebench >= 5.0 |
| `swe-bench-multimodal` | `SWE-bench/SWE-bench_Multimodal` | swebench >= 5.0 |
| `swe-bench-pro` | `ScaleAI/SWE-bench_Pro` (pinned to `v1.0`) | — |

Spelling is forgiving (`"SWE-bench Pro"`, `swebench_pro`, `pro` all work). A
local `.jsonl` / `.json` path also works; set `dataset.benchmark` to
`swe-bench` or `swe-bench-pro` if it cannot be inferred.

**SWE-bench Pro differences**, all handled automatically from
`dataset.benchmark`: images come from `docker.io/jefzda/sweap-images`, the repo
is at `/app`, the prompt concatenates `problem_statement` + `requirements` +
`interface`, and grading uses the per-instance `run_script.sh` / `parser.py`
from `scaleapi/SWE-bench_Pro-os` (fetched and cached under
`~/.cache/orchard-eval/swebench-pro/`, or set `grading.pro_scripts_dir` for an
offline run).

### Terminal-Bench (Harbor)

```bash
harbor-orchard audit <task-dir>                          # offline: what will run
./scripts/harbor_oracle_check.sh                         # replay reference solutions (~99% on 2.1)
./scripts/run_terminal_bench.sh codex 32
orchard-eval harbor -d terminal-bench/terminal-bench-2-1@latest \
    --agent codex --model "openai/$MODEL_NAME" -n 32
```

Use 2.1 to bring the provider up (almost all single-container tasks), then 4.0
(`terminal-bench/terminal-bench@4.0.0`, expected oracle rate ~80%) which uses a
separate verifier environment on every task.

### DeepSWE 1.1 (Harbor)

```bash
./scripts/deep_swe_oracle_check.sh 16 --limit 5     # ceiling, expect ~100%
./scripts/run_deep_swe.sh pi 8 --limit 5            # smoke
./scripts/run_deep_swe.sh pi 32                     # full
```

- **Air-gapped agent phase.** Tasks declare `network_mode = "no-network"`; the
  provider switches the pod to isolated before the agent runs, keeping only the
  model endpoint reachable (add extra hosts via `ORCHARD_HARBOR_EGRESS_ALLOW`).
  Run `oracle` with `ORCHARD_HARBOR_MODEL_EGRESS=0`.
- **Grading in a second container.** The agent must **commit** its work; the
  diff is replayed into a pristine image built from `tests/Dockerfile`.
- **Timeout.** `ORCHARD_HARBOR_EXEC_TIMEOUT=7200` keeps a trial inside the
  orchestrator's default 2h sandbox TTL; raise both together for the full
  10800s budget.
- **Images** are remapped from ECR to `mirror.gcr.io/wenlinyao/deep-swe`.
  `ORCHARD_HARBOR_IMAGE_REMAP=` disables it; mirror your own with
  `DEST_REPO=yourorg/deep-swe ./scripts/mirror_deep_swe_images.sh`.

### SWE-bench Pro V1 via Harbor

Measures the same 731 V1 instances through Harbor's independent pipeline
(grading in the agent's own container). Comparing the two paths is evidence
that neither is broken:

```bash
python scripts/compare_swebench_pro.py \
    results/mini-swe-agent-swebench-pro results/harbor/mini-swe-agent-swebench-pro-harbor
python scripts/compare_swebench_pro.py --emit-harbor-tasks results/mini-swe-agent-swebench-pro  # same task set
```

For parity the scripts pass `--override-cpus 4 --override-memory-mb 16384`.
Expect a small "harbor only" surplus, since Harbor grades in the agent's own
container. Pin the dataset as `@2` (a revision number); `@2.0.0` is parsed as
a tag and fails.

### SWE-bench Pro V2 (Harbor)

642 tasks with sanitised git history, an air-gapped agent phase, and a
re-grade in a fresh sandbox. **The re-graded number is the one to report.**

```bash
./scripts/fetch_swebench_pro_v2.sh                     # pinned + checksum-verified
export PRO_V2_DIR=.../third_party/SWE-bench_Pro-os

harbor-orchard audit "$PRO_V2_DIR/v2/tasks"            # offline, expect 642/642
SAMPLE=25 ./scripts/run_swebench_pro_v2.sh oracle 16 --threshold 1.0
SAMPLE=25 ./scripts/run_swebench_pro_v2.sh nop    16 --max-solve-rate 0.0

./scripts/run_swebench_pro_v2.sh mini-swe-agent 24                         # solve
REGRADE=results/harbor/mini-swe-agent-swebench-pro-v2-<stamp> \
    ./scripts/run_swebench_pro_v2.sh mini-swe-agent 24                     # re-grade
HARD51=1 ./scripts/run_swebench_pro_v2.sh mini-swe-agent 16                # hard subset
```

- Use `SAMPLE=N` rather than `--limit N`: `--limit` takes the first N sorted
  tasks (all one repo); `SAMPLE` spreads across all 11 repos.
- Don't set `ORCHARD_HARBOR_FORCE_ISOLATION` — V2 declares its own air-gap.
- A 0% re-grade means no `model.patch` was captured; check that
  `ORCHARD_HARBOR_STOCK_AGENTS` was unset on the solve pass.
- `swebench_pro_v2_leakprobe.py` / `swebench_pro_v1_leakprobe.py` check that
  the air-gap holds and the history is sanitised.
- V2 scores are not comparable with V1 (different tasks and requirements).

## Sanity checks

Run these before trusting any model score. Each caps (or floors) every number
measured afterwards, and groups failures by cause.

| Check | Expect | Command |
| --- | --- | --- |
| Gold patch (ceiling) | ~100% | `python scripts/gold_patch_check.py --limit 50` |
| No patch (floor) | ~0% | `python scripts/no_patch_check.py --limit 50` |
| Harbor oracle | ~99% on TB 2.1 | `./scripts/harbor_oracle_check.sh` |
| Harbor nop | ~0% | `orchard-eval harbor -d <dataset> --agent nop --max-solve-rate 0.02` |
| Harness smoke | runs end to end | `./scripts/smoke_test.sh codex 3` |

Add `--benchmark swe-bench-pro` or `dataset.name=swe-bench-multilingual` to the
gold / no-patch checks for other datasets.

- **`gold`** replays the dataset's human fix through the full pipeline,
  including patch extraction. Failure causes: `agent_error` (did not apply /
  extraction empty), `EMPTY_PATCH`, `EVAL_INFRA_ERROR`, `EVAL_TIMEOUT`,
  `EVAL_RESET_FAILED`. `harness.params.extract=false` grades the dataset
  column verbatim.
- **`noop`** submits nothing and reports three numbers: the resolve rate (~0%),
  tests that actually ran (~100%), and `PASS_TO_PASS` green (~100%). A non-zero
  `residual_patch_bytes` means the image leaves state in the working tree.

## Harnesses

```console
$ orchard-eval list-harnesses
  claude-code     Anthropic Claude Code (`claude -p`), running inside the sandbox
  codex           OpenAI Codex CLI (`codex exec`), running inside the sandbox
  gold            Replays the dataset's own patch — upper bound / sanity check
  mini-swe-agent  mini-swe-agent (`mini`), running inside the sandbox
  noop            Submits nothing — lower bound / sanity check
  opencode        OpenCode (`opencode run`), running inside the sandbox
  pi              pi coding agent (`pi -p`), running inside the sandbox
```

CLI harnesses run entirely inside the pod with their own sandboxing disabled
(the pod is already isolated). The suite stages the prompt as a file, stages a
provider config pointing at `MODEL_BASE_URL` (saved as `agent_config.*`), runs
one command, and extracts the patch with `git diff`.

- **codex** — `--wire-api responses` (default) or `chat` for servers without
  `/v1/responses`.
- **mini-swe-agent** — uses its builtin `benchmarks/swebench.yaml`, so scores
  are comparable with upstream. Override settings with
  `harness.params.config_overrides='["agent.step_limit=100"]'`. Self-installs
  into `/var/tmp/orchard-mini` if the tools image predates it
  (`harness.params.auto_install=false` to fail fast).
- **claude-code** — speaks the Anthropic Messages API (`/v1/messages`), which
  sglang serves natively. See [claude-code notes](#claude-code). Its config
  gives the agent 3600s versus 1800s for the others.

Adding a CLI agent is a declarative `CliSpec`:

```python
@register_harness
class MyAgentHarness(InstalledCliHarness):
    name = "my-agent"
    description = "My agent, running inside the sandbox"
    spec = CliSpec(
        binary="myagent",
        args=(("run",), ("--model", "{model}"), (PROMPT_TOKEN,)),
        prompt_delivery="argv",
        api_key_env="MY_API_KEY",
        base_url_env="MY_BASE_URL",
    )
```

A custom (non-CLI) harness implements one method; sandbox lifecycle,
concurrency, retries, grading and persistence are the runner's job:

```python
@register_harness
class MyHarness(Harness):
    name = "my-harness"

    async def rollout(self, ctx: RolloutContext) -> RolloutResult:
        result = await ctx.sandbox.exec("...", cwd=ctx.workdir)
        patch = await ctx.sandbox.extract_patch(ctx.instance.base_commit)
        return RolloutResult(patch=patch, messages=[...], stdout=result.stdout)
```

## Configuration

Three layers, in increasing precedence: model defaults → YAML files (`-c`,
repeatable) → dotted `key=value` overrides. Common knobs also have flags
(`--limit`, `--concurrency`, `--model`, `--run-name`, ...).

```bash
orchard-eval run -c configs/codex.yaml dataset.limit=10 concurrency=5
orchard-eval run -c configs/codex.yaml harness.name=pi
orchard-eval run -c configs/mini-swe-agent.yaml \
    model.name=openai/Qwen3-32B model.base_url=http://vllm-host:8000/v1
orchard-eval run -c configs/pi.yaml --instance-id django__django-11095,astropy__astropy-12907 --no-resume
```

Config strings may reference the environment (`${MODEL_BASE_URL}`); an unset
variable is a load-time error.

- **Credentials.** Model keys are read via `model.api_key_env` and passed to
  the agent per call — never baked into an image or written to the run
  directory. Sandbox credentials come from `SANDBOX_BASE_URL` /
  `SANDBOX_API_KEY`.
- **Registry.** `sandbox.image_prefix` re-points standard SWE-bench images at a
  mirror, e.g. `sandbox.image_prefix=myregistry.example.com/swebench`, to avoid
  Docker Hub rate limits.

## Model serving and routing

The suite pins each rollout (or Harbor trial) to one engine so its prefix cache
survives across turns:

| Mode | Endpoint | Pinning |
| --- | --- | --- |
| `--routing session` | one router port (`scripts/session_router.py`) | router places each new session on the least-loaded engine; URL is `.../session/<id>/v1` |
| `--routing sticky` (default) | N consecutive ports, `--base-url-replicas N` | hash of `instance_id` |
| `--routing random` | same as sticky | random per rollout |
| `model.base_urls: [...]` | explicit list | — |

`run_all_evals.sh` defaults to `MODEL_ROUTING=session` with `REPLICAS=1`. The
suite sends `DELETE /router/session/<id>` at teardown so load is tracked
exactly; if the published port is not reachable from the driver, set
`ROUTER_CONTROL_URL=http://127.0.0.1:8100` (the script auto-detects a local
router).

Check the spread:

```bash
curl -s http://127.0.0.1:8100/router/stats | jq '.spread, .sessions, .load_in_use'   # session
jq -r '.metrics.base_url' "$RUN/results.jsonl" | sort | uniq -c                       # sticky
```

Without the router, publish every engine through frp (`REPLICAS=8`, no
`BASE_PORT`) and make sure `frps.toml`'s `allowPorts` covers the whole range.

## Output and trajectories

```
results/<run_name>/<run_id>/
├── results.jsonl              # one InstanceRecord per line, appended as produced
├── summary.json               # resolve rate, exit statuses, cost, timings
├── preds.json                 # SWE-bench prediction format
└── instances/<instance_id>/
    ├── prompt.txt             # exactly what the agent was told
    ├── agent.log              # raw harness output
    ├── agent.stderr.log       # transport errors (401s, bad routes)
    ├── agent_config.*         # provider config staged in the pod
    ├── trajectory.raw.jsonl   # the CLI's own event stream, verbatim
    ├── trajectory.json        # normalized turn-by-turn record
    ├── patch.diff             # what was graded
    ├── eval.log               # raw test output
    └── record.json
```

Harbor jobs write to `results/harbor/<job>/<task>__<id>/` in Harbor's own
layout. `run_all_evals.sh` writes under `results/<MODEL_TAG>/` and
`logs/<MODEL_TAG>/`, with a `$RUN_ID` per sweep so re-runs never overwrite.

- `<run_id>` is a `YYYYmmdd-HHMMSS` stamp; `--run-id` re-opens a specific
  attempt, `--output-dir` changes the root.
- Re-grade independently: `python -m swebench.harness.run_evaluation
  --predictions_path .../preds.json --run_id recheck`, or cheaply from existing
  logs with `python scripts/regrade.py results/<run_name>/<run_id>`.
- **Trajectories** are written for every harness, normalized to
  mini-swe-agent's message shape. Unknown events are preserved as
  `extra.kind == "unknown"`, and a parser crash never fails a rollout.
  `n_messages` (per record) and `missing_trajectories` (in `summary.json`)
  make silent capture failures visible. Disable with
  `harness.params.capture_trajectory=false`.
- A healthy run has `n_messages > 0`. Empty `n_messages` next to a non-empty
  `trajectory.raw.jsonl` means the model call failed — see
  `agent.stderr.log`.

## Grading

Native runs grade in a **fresh eval pod**, never the agent's own: reset to
`base_commit`, apply the patch with swebench's `GIT_APPLY_CMDS`, run the
instance's `eval_script`, and parse with swebench's own parser (Pro uses
upstream's `run_script.sh` + `parser.py`). This matches upstream SWE-bench, so
anything the agent did outside the diff (installs, caches, stray files) earns
nothing. An eval pod that won't start is retried, then recorded as
`EVAL_INFRA_ERROR`. Empty patches skip grading unless
`grading.grade_empty_patch=true`.

## Resume and failure handling

`results.jsonl` is appended as each instance finishes, and re-running the same
command resumes, skipping recorded instances (except `infra_error` records,
which are retried; `retry_infra_on_resume=false` to disable).

- **Infrastructure failures** (pod didn't start, pod reaped, orchestrator 5xx,
  in-pod agent unresponsive) re-run the whole instance on a new pod, up to
  `rollout_retries` (default 2) times, before recording `infra_error`.
  408/409/425/429 are retried; other 4xx are not.
- **Agent failures** (crash, empty patch, patch won't apply, tests fail) are
  results and never retried.

To replace a block of records after the fact:

```bash
RUN=results/<TAG>/pi-swebench-pro/<RUN_ID>
python scripts/merge_results.py --select "cannot execute in this image" "$RUN" > /tmp/redo.txt
orchard-eval run -c configs/pi.yaml -c configs/swebench-pro.yaml \
    --output-dir results/<TAG> --run-name pi-swebench-pro-rerun \
    --instance-id "$(paste -sd, /tmp/redo.txt)" --no-resume
python scripts/merge_results.py "$RUN" --overlay results/<TAG>/pi-swebench-pro-rerun/<RUN_ID>
```

The merge lands in `<RUN_ID>-merged` (`--in-place` to overwrite). For Harbor
jobs, `python scripts/harbor_failed_tasks.py results/harbor/<job>` sorts
failures by cause and `--signature <cause>` emits a retry list for
`--task-file`.

## Debugging a failed trial

Start with one instance and full logs:

```bash
orchard-eval run -c configs/codex.yaml --instance-id astropy__astropy-12907 \
    --no-grading --no-resume --log-level DEBUG
orchard-eval harbor -d terminal-bench/terminal-bench-2-1@latest \
    --agent oracle --limit 1 -n 1 --log-level DEBUG
```

For a Harbor trial (`results/harbor/<job>/<task>__<id>/`):

| File | Purpose |
| --- | --- |
| `trial.log` | Image chosen, build steps, phase boundaries, exceptions — read first |
| `result.json` | Rewards, `exception_info`, per-phase timings |
| `exception.txt` | Traceback, if the trial raised |
| `agent/` | Downloaded from `/logs/agent` (agent logs; `oracle.txt` for oracle) |
| `verifier/` | Downloaded from `/logs/verifier` (`test-stdout.txt`, `reward.txt`) |
| `artifacts/` | Declared artifacts plus `manifest.json` |

Orchard cannot bind-mount, so `agent/`, `verifier/` and `artifacts/` are
downloaded from the pod after each phase. `harbor-orchard translate <task>
--show-env` shows what a task's Dockerfile becomes, offline.

## Known caveats

### claude-code

- The credential goes in `ANTHROPIC_AUTH_TOKEN` (Bearer), not
  `ANTHROPIC_API_KEY` (`X-Api-Key`, which sglang ignores). It defaults to
  `EMPTY` so the CLI doesn't look for OAuth login.
- `ANTHROPIC_BASE_URL` drops the trailing `/v1` (the CLI appends
  `/v1/messages`).
- Extended thinking is off (`max_thinking_tokens: 0`); sglang rejects
  unsigned thinking blocks replayed in history.
- The session router strips `output_config.effort` so the model runs at its
  chat template's default; `effort_fallback: medium` applies only without the
  router (`--no-strip-effort` disables stripping).

In the sglang access log, `POST /session/<id>/v1/messages` is correct;
`/v1/v1/messages` means the `/v1` strip failed, and a 401 means the token was
sent as `X-Api-Key`.

### Reasoning effort

Qwen3.8's chat template accepts only `xhigh` (the default), `medium` and `low`;
anything else (including `high`) fails with HTTP 400/500. All harnesses
therefore send no effort and run at `xhigh`. Harbor's `codex` agent hard-codes
`high`, so `run_all_evals.sh` passes `--ak reasoning_effort=null` on those
stages. To spend fewer tokens set `harness.params.reasoning_effort` (codex) to
`medium` or `low`.