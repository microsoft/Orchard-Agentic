"""mini-swe-agent, run inside the sandbox like every other CLI agent.

``mini`` ships in every Orchard Env sandbox alongside ``codex`` and ``pi``, so
this harness has the same shape as those: stage a prompt, issue one command,
read the result back. The whole agent loop — model calls included — happens on
the sandbox pod, which is what makes a run scale with the cluster rather than
with the machine that launched it, and what keeps the model endpoint a
pod-to-server hop instead of a round trip through the evaluation host.

What is *not* changed is mini-swe-agent itself: its own
``benchmarks/swebench.yaml`` config, prompts, step budget and submission
protocol are used unmodified, so a score here stays comparable with an upstream
mini-swe-agent run.

Two things about ``mini`` differ from the other CLIs and drive the spec below:

* its trajectory goes to a **file** (``-o``), because stdout carries a
  human-readable log rather than an event stream;
* its SWE-bench prompt makes the model produce and submit the diff itself, so
  that submission — not the working tree — is the fairest thing to grade.

``mini`` is also the newest addition to the sandbox tools image, so this harness
carries an install fallback for pods still running an older one.
"""

from __future__ import annotations

import shlex

from orchard_evalkit.harnesses.base import RolloutContext, register_harness
from orchard_evalkit.harnesses.installed_cli import (
    PROMPT_TOKEN,
    CliSpec,
    InstalledCliHarness,
)

#: Sentinel mini-swe-agent's SWE-bench prompt tells the model to echo when done.
SUBMISSION_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

#: mini-swe-agent's own SWE-bench config, used unless overridden.
DEFAULT_CONFIG_SPEC = "benchmarks/swebench.yaml"

#: Seconds one bash command may run before mini kills it. Long enough for a
#: compiled language's build and test run, and the same as the Harbor path.
DEFAULT_STEP_TIMEOUT_S = 120

#: Writable state directory for mini inside the sandbox. mini creates its global
#: config dir at *import* time, so pointing ``MSWEA_GLOBAL_CONFIG_DIR`` here is
#: what keeps the CLI working in images whose HOME is read-only or absent.
MINI_HOME = "/var/tmp/orchard-mini"

#: Where ``mini -o`` writes its native trajectory JSON inside the sandbox.
MINI_TRAJECTORY_PATH = f"{MINI_HOME}/trajectory.json"

#: Endpoint routing for mini, staged as an extra config layer.
#:
#: ``model_kwargs`` is forwarded verbatim to litellm, so a self-hosted
#: vLLM/SGLang server needs nothing but ``api_base``. This is preferred over
#: relying on OPENAI_BASE_URL alone because which endpoint environment variable
#: litellm honours has changed between releases, whereas ``api_base`` is the
#: argument all of them ultimately resolve to.
MINI_CONFIG_TEMPLATE = """\
model:
  model_kwargs:
    api_base: {base_url}
"""

#: Version range this harness's command line is written against (mini v2).
MINI_VERSION_SPEC = "mini-swe-agent>=2.4,<3"

#: Repository path mini's builtin SWE-bench config assumes.
#:
#: ``benchmarks/swebench.yaml`` hardcodes it twice: as ``environment.cwd``, which
#: the spec below overrides, and as prose inside ``agent.instance_template``
#: ("MODIFY: Regular source code files in /testbed"), which it cannot — a `-c`
#: override would have to carry a verbatim copy of upstream's whole template
#: into this repo, and that copy would drift from the pinned version in silence.
#: SWE-bench Pro checks out at ``/app``, so on Pro every rollout was being told
#: to edit a directory that does not exist.
MINI_BUILTIN_WORKDIR = "/testbed"

#: Correction prepended to the task when the repository is somewhere else.
#: Fixing it from the task side needs no knowledge of the template at all.
MINI_WORKDIR_NOTE = """\
Paths: this repository is checked out at {workdir}, which is already your
working directory. Anything below that refers to {builtin} means {workdir} —
there is no {builtin} directory in this environment.

"""

#: Fallback for a sandbox whose tools image predates the bundled ``mini``.
#:
#: Everything lands in an isolated venv under :data:`MINI_HOME`. Installing into
#: the image's own interpreter is not an option: on a SWE-bench image that
#: interpreter's site-packages *is* the package under test, and mini's
#: dependency set (litellm, datasets, pydantic) would silently resolve the
#: benchmark out from under the agent.
#:
#: Picking the interpreter is the whole difficulty. Bare ``python3`` resolves to
#: the ``testbed`` conda env on a SWE-bench image, which is routinely older than
#: the 3.10 mini requires, so named versions and the two interpreters those
#: images reliably carry are tried ahead of it.
#:
#: This needs egress to PyPI and adds a couple of minutes to the first rollout
#: on each pod; rebuilding the sandbox-tools image removes it entirely.
MINI_INSTALL_COMMAND = rf"""
set -eu
VENV={MINI_HOME}/venv
SPEC='{MINI_VERSION_SPEC}'

install_with_uv() {{
    command -v uv >/dev/null 2>&1 || return 1
    uv venv --quiet --python '>=3.10' "$VENV" || return 1
    uv pip install --quiet --python "$VENV/bin/python" "$SPEC"
}}

install_with_venv() {{
    for candidate in python3.13 python3.12 python3.11 python3.10 \
                     /usr/bin/python3 /opt/miniconda3/bin/python3 python3; do
        py=$(command -v "$candidate" 2>/dev/null) || continue
        "$py" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || continue
        rm -rf "$VENV"
        "$py" -m venv "$VENV" || continue
        "$VENV/bin/python" -m pip install --quiet --upgrade pip || continue
        "$VENV/bin/python" -m pip install --quiet "$SPEC" && return 0
    done
    return 1
}}

if [ ! -x "$VENV/bin/mini" ]; then
    mkdir -p {MINI_HOME}
    install_with_uv || install_with_venv || true
fi
test -x "$VENV/bin/mini"

mkdir -p /usr/local/bin
printf '#!/bin/sh\nunset PYTHONHOME PYTHONPATH\nexec "%s/bin/mini" "$@"\n' \
    "$VENV" > /usr/local/bin/mini
chmod 0755 /usr/local/bin/mini
"""


@register_harness
class MiniSweAgentHarness(InstalledCliHarness):
    """mini-swe-agent's ``mini`` CLI, run non-interactively inside the sandbox.

    Recognized ``harness.params``, beyond the ones every CLI harness takes:

    ``config``
        mini-swe-agent config spec (path, builtin name, or ``key=value``).
        Defaults to its own ``benchmarks/swebench.yaml``.
    ``config_overrides``
        Extra specs merged *over* that config, e.g.
        ``["agent.step_limit=100", "agent.cost_limit=2.0"]``.
    ``step_timeout``
        Seconds for a single bash command inside the sandbox (default 120,
        the same as ``harbor_orchard.agents.MiniSweAgent``).
    ``temperature``
        Sampling temperature mini sends with every request. Unset, it sends
        none and the server's default applies — for a model served with
        SGLang's default ``--sampling-defaults model``, the checkpoint's
        ``generation_config.json``.

    When the pod's tools image is older than the bundled ``mini``, the CLI is
    installed into the pod on first use — see :data:`MINI_INSTALL_COMMAND` and
    the shared ``auto_install`` parameter.

    On a benchmark that checks out somewhere other than ``/testbed`` the task is
    prefixed with :data:`MINI_WORKDIR_NOTE`, because the builtin config's prompt
    names ``/testbed`` in prose that no ``-c`` override can reach.
    """

    name = "mini-swe-agent"
    description = "mini-swe-agent (`mini`), running inside the sandbox"
    spec = CliSpec(
        binary="mini",
        args=(
            # `mini` defaults to its interactive agent, which would block on a
            # confirmation prompt that nothing here can ever answer.
            ("--agent-class", "default"),
            # Order matters: each -c is merged over the previous one.
            ("-c", "{config}"),
            # swebench.yaml targets DockerEnvironment, which would try to launch
            # a container from inside the pod. The pod already is the container.
            ("-c", "environment.environment_class=local"),
            ("-c", "environment.cwd={workdir}"),
            ("-c", "environment.timeout={step_timeout}"),
            # The agent's own wall-clock guard, so a stuck rollout ends inside
            # the agent — with its trajectory saved — rather than being killed
            # from outside when the harness timeout expires.
            ("-c", "agent.wall_time_limit_seconds={timeout}"),
            ("-c", "{model_config}"),
            ("-m", "{model}"),
            ("-t", PROMPT_TOKEN),
        ),
        prompt_delivery="argv",
        trajectory_args=(("-o", "{trajectory}"),),
        trajectory_format="mini",
        trajectory_path=MINI_TRAJECTORY_PATH,
        api_key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",
        env={
            # Without this, mini runs its first-time setup wizard and blocks on
            # a prompt for a model name and an API key.
            "MSWEA_CONFIGURED": "true",
            "MSWEA_SILENT_STARTUP": "1",
            "MSWEA_GLOBAL_CONFIG_DIR": MINI_HOME,
            # A self-hosted model is never in litellm's price table, and mini
            # turns that lookup miss into a RuntimeError that kills the rollout.
            "MSWEA_COST_TRACKING": "ignore_errors",
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
            # Overridden by the real credential when there is one. litellm
            # refuses to call an OpenAI-compatible endpoint with no key at all,
            # and a server started without --api-key accepts any value.
            "OPENAI_API_KEY": "EMPTY",
        },
        setup_command=f"mkdir -p {MINI_HOME}",
        install_command=MINI_INSTALL_COMMAND,
        config_path=f"{MINI_HOME}/model.yaml",
        config_template=MINI_CONFIG_TEMPLATE,
        # mini's local environment shells out through `sh`, which never reads
        # ~/.bashrc — so on a SWE-bench image the agent's commands would run
        # against the base conda env instead of `testbed`. Starting mini itself
        # from a login shell puts the right toolchain into the environment every
        # child process inherits.
        login_shell=True,
        # swebench.yaml already wraps the issue in mini's own instance template;
        # anything added here would be a second, conflicting set of directions.
        prompt_template="{problem_statement}",
        patch_from_submission=True,
        # mini is a typer app with no --version; --help exercises the same
        # interpreter and import path, which is what breaks when the bundled
        # CPython cannot run in the task image.
        version_args=("--help",),
    )

    def _render_prompt(self, ctx: RolloutContext) -> str:
        prompt = super()._render_prompt(ctx)
        # Only the builtin config makes the /testbed claim; a caller who brought
        # their own has already said where the repository is.
        if self.params.get("config") or ctx.workdir == MINI_BUILTIN_WORKDIR:
            return prompt
        note = MINI_WORKDIR_NOTE.format(
            workdir=ctx.workdir, builtin=MINI_BUILTIN_WORKDIR
        )
        return note + prompt

    def _substitutions(self, ctx: RolloutContext) -> dict[str, str]:
        return {
            **super()._substitutions(ctx),
            "config": str(self.params.get("config") or DEFAULT_CONFIG_SPEC),
            "step_timeout": str(self.params.get("step_timeout", DEFAULT_STEP_TIMEOUT_S)),
            # Empty without a base_url, which drops the whole `-c` group: mini
            # would otherwise fail on a config file that was never staged.
            "model_config": (
                self.spec.config_path
                if self._render_config(self._model_for(ctx)) is not None
                else ""
            ),
        }

    def _extra_args(self) -> list[str]:
        overrides: list[str] = []
        temperature = self.params.get("temperature")
        if temperature not in (None, ""):
            # Ahead of config_overrides, so a temperature set there still wins.
            overrides.extend(
                ["-c", f"model.model_kwargs.temperature={float(temperature)}"]
            )
        for override in self.params.get("config_overrides") or []:
            overrides.extend(["-c", shlex.quote(str(override))])
        # Last wins in mini's merge, so user overrides come after everything the
        # spec set.
        return overrides + super()._extra_args()


__all__ = [
    "DEFAULT_CONFIG_SPEC",
    "MINI_BUILTIN_WORKDIR",
    "MINI_CONFIG_TEMPLATE",
    "MINI_HOME",
    "MINI_INSTALL_COMMAND",
    "MINI_TRAJECTORY_PATH",
    "MINI_VERSION_SPEC",
    "MINI_WORKDIR_NOTE",
    "MiniSweAgentHarness",
    "SUBMISSION_SENTINEL",
]
