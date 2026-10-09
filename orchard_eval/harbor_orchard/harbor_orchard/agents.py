"""Harbor agents that use the CLI the pod already has.

Harbor's installed agents fetch their CLI at trial time, from inside the task
image: ``pi`` through nvm and npm, ``mini-swe-agent`` through ``uv tool
install``. Orchard sandboxes do not need that — the orchestrator mounts a
prebuilt, self-contained payload at ``/opt/sandbox-tools`` and shims it into
``/usr/local/bin`` in every pod — and on SWE-bench Pro the fetch is not merely
redundant, it is the single largest source of lost trials:

``pi``
    Harbor's ``Pi.install()`` always runs the nvm snippet, whose own docstring
    says it "requires ... a glibc-based distro: official Node binaries
    downloaded by nvm do not run on musl". 88 of the 731 Pro images are Alpine,
    nvm asks nodejs.org for ``node-*-linux-x64-musl.tar.gz``, gets a 404, and
    every one of those trials dies with exit 127 — which Harbor's classifier
    then reports as ``NetworkConnectionError``.

``mini-swe-agent``
    Harbor's ``MiniSweAgent.install()`` resolves mini's whole dependency tree
    against whatever Python the task image happens to carry. On the Pro images
    that fails two ways: uv falls back to an sdist of a Rust-backed dependency
    and the image has no Rust toolchain, or the tool installs against a system
    Python 3.10 and then dies importing ``typing.NotRequired``. 88 trials,
    again.

``codex`` needs nothing here: Harbor's codex agent already short-circuits when
``codex`` is on PATH, and the payload's codex is the static-PIE musl build. It
is subclassed anyway, for the patch capture below and for nothing else.

``claude``
    Harbor's ``ClaudeCode.install()`` short-circuits too, but on ``command -v
    claude`` — a PATH lookup. The payload's ``claude`` is a glibc ELF, so on a
    musl image it resolves, Harbor calls the agent installed, and the trial dies
    on a loader error instead. It is subclassed for that distinction rather than
    to skip an install.

All four refuse to start on a repository that is already modified — see
:class:`_CleanTreeGate` — and write ``/logs/agent/model.patch`` when the agent
stops — see :class:`_ModelPatchCapture`. The second is what lets a run be
re-graded in a fresh sandbox instead of in the container the agent just spent
an hour mutating, which is the protocol SWE-bench Pro V2 asks for.

Each subclass below makes the same decision in ``install()``:

1. the payload CLI *runs*, and no version was pinned — skip Harbor's install
   entirely;
2. it does not run and the image is musl — fail with that as the reason, rather
   than letting Harbor's installer fail for a reason of its own;
3. otherwise — defer to Harbor, so an image with no payload mounted, or a run
   that pinned a version the payload cannot supply, behaves as it does upstream.

Step 1 runs the CLI rather than looking for it: a glibc payload on a musl host
resolves on PATH and then fails to ``execve``, so ``command -v`` cannot tell the
two apart. That distinction is the whole point of this module.

Harbor loads these by import path, which ``orchard-eval harbor`` fills in for
the short agent names::

    harbor run --agent harbor_orchard.agents:Pi ...
"""

from __future__ import annotations

import shlex

from harbor.agents.installed.claude_code import ClaudeCode as _ClaudeCode
from harbor.agents.installed.codex import Codex as _Codex
from harbor.agents.installed.mini_swe_agent import MiniSweAgent as _MiniSweAgent
from harbor.agents.installed.pi import Pi as _Pi
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harbor_orchard.settings import (
    AGENT_KILL_GRACE_S,
    CLEAN_TREE_TIMEOUT_S,
    DEADLINE_ENV_VAR,
    MODEL_PATCH_TIMEOUT_S,
    PAYLOAD_DIR,
    REQUIRE_CLEAN_TREE_ENV_VAR,
    require_clean_tree,
)
from harbor_orchard.shell import (
    CLEAN_TREE_PREFIX,
    MODEL_PATCH_PATH,
    clean_tree_routine,
    deadline_shim,
    model_patch_routine,
)

__all__ = [
    "ClaudeCode",
    "Codex",
    "DirtyWorkingTreeError",
    "MiniSweAgent",
    "Pi",
    "PayloadUnusableError",
]

#: Where the orchestrator mounts the sandbox-tools payload
#: (``SANDBOX_TOOLS_MOUNT_PATH``). Probed directly rather than through PATH so
#: the answer does not depend on which shell Harbor's exec happened to use.
#: Defined in ``settings`` because the environment matches on it too, when it
#: has to stop an agent Harbor has stopped waiting for.
TOOLS_DIR = PAYLOAD_DIR

#: True when the image's libc is musl. Both tests are needed: a
#: ``FROM node:*-alpine`` image has the release file, and a musl image built
#: from something else still answers ``ldd --version``.
IS_MUSL = "[ -f /etc/alpine-release ] || ldd --version 2>&1 | head -1 | grep -qi musl"


class PayloadUnusableError(RuntimeError):
    """The pod's agent CLI cannot run here, and neither can Harbor's installer.

    Raised instead of letting the upstream installer fail on its own terms,
    because its failure names the symptom (a 404 from nodejs.org, a missing
    ``rustc``) rather than the cause.
    """


#: ``stdbuf`` is GNU coreutils, which busybox does not carry, and Harbor pipes
#: several agents' output through ``stdbuf -oL tee``. On an Alpine image that
#: stage exits 127, ``set -o pipefail`` propagates it, and the trial is recorded
#: as the agent having failed — after the model has been paid for.
#:
#: The shim drops the buffering flags and runs the command. All that is lost is
#: line-buffering of a log that is being written to a file anyway.
STDBUF_SHIM = """#!/bin/sh
# Injected by harbor_orchard: this image has no GNU coreutils.
while [ $# -gt 0 ]; do
    case "$1" in
        -i|-o|-e|--input|--output|--error)
            shift
            if [ $# -gt 0 ]; then shift; fi
            ;;
        -i*|-o*|-e*|--input=*|--output=*|--error=*) shift ;;
        --) shift; break ;;
        *) break ;;
    esac
done
[ $# -gt 0 ] || exit 0
exec "$@"
"""


def _probe(binary: str, flag: str) -> str:
    """A command that succeeds only if *binary* actually executes.

    Output is discarded and stderr is kept out of Harbor's log: this is asked
    once per trial and a failure is expected on musl images, where the useful
    message is the one raised from Python, not a loader error.
    """
    quoted = shlex.quote(binary)
    return f"[ -x {quoted} ] && {quoted} {flag} >/dev/null 2>&1"


class DirtyWorkingTreeError(RuntimeError):
    """The agent's repository was already modified before the agent started.

    Raised instead of running the agent, because the alternative is a trial
    that scores normally while measuring something else.
    """


class _CleanTreeGate:
    """Refuse to start an agent on a checkout someone has already changed.

    A benchmark number is a solve rate *at a fixed budget from a fixed starting
    point*. On the 2026-09-24 SWE-bench Pro V2 sweep 96 of 642 trials did not
    have that starting point: their agent opened a repository in which the
    task's own target files were already modified, and in the trial inspected
    line by line the previous occupant had finished the task — the second agent
    read the code, ran the tests, and submitted. Nothing failed, so the run
    reported 92.68%.

    The cause was below this package, in the sandbox SDK: ``POST /exec`` was
    re-submitted when the HTTP call carrying the *result* died, which started a
    second CLI beside the first rather than resuming it. Commands now go through
    :mod:`orchard_evalkit.jobs`, which sends one once and waits on its job id,
    and a lost connection fails the trial with ``SandboxInfraError`` so Harbor
    reruns it in a new pod. This gate runs before the first launch only, so it
    never saw that re-send — it stays as the guard for every other way a pod
    could arrive used.

    Mixed in ahead of :class:`_ModelPatchCapture` so a refused trial writes no
    ``model.patch``: there is no rollout to re-grade, and a patch of someone
    else's work is worse than none.
    """

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        await self._assert_clean_tree(environment)
        await super().run(instruction, environment, context)  # type: ignore[misc]

    async def _assert_clean_tree(self, environment: BaseEnvironment) -> None:
        """Raise :class:`DirtyWorkingTreeError` if the repository is modified.

        Every outcome other than "definitely dirty" lets the trial proceed. A
        check that cannot run is not evidence of anything, and failing rollouts
        over it would trade a known problem for an unknown one.
        """
        if not require_clean_tree():
            return
        try:
            result = await self.exec_as_root(  # type: ignore[attr-defined]
                environment,
                command=clean_tree_routine(),
                timeout_sec=CLEAN_TREE_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - see the method docstring
            self.logger.warning(  # type: ignore[attr-defined]
                "could not check whether the repository is clean, so this "
                "trial starts unverified: %s",
                exc,
            )
            return

        stdout = (getattr(result, "stdout", "") or "").strip()
        report = next(
            (
                line
                for line in stdout.splitlines()
                if line.startswith(CLEAN_TREE_PREFIX)
            ),
            "",
        )
        state = report[len(CLEAN_TREE_PREFIX) :].split(" ", 1)[0].rstrip(":")
        if state == "clean":
            self.logger.info("%s", report)  # type: ignore[attr-defined]
            return
        if state != "dirty":
            self.logger.warning(  # type: ignore[attr-defined]
                "could not tell whether the repository is clean, so this "
                "trial starts unverified: %s",
                report or stdout or "the check printed nothing",
            )
            return
        raise DirtyWorkingTreeError(
            f"{stdout}\n"
            "The agent has not run yet, so these changes are not its own: "
            "something else worked in this pod first. This trial would not "
            "measure the same thing as its neighbours, so it is failed here "
            "rather than scored. Re-run it; if this dataset's images ship "
            f"modifications deliberately, set {REQUIRE_CLEAN_TREE_ENV_VAR}=0."
        )


class _ModelPatchCapture:
    """Write ``model.patch`` once the agent stops. Mixed in ahead of Harbor's.

    SWE-bench Pro V2's locked protocol reports the score a *fresh* sandbox
    gives the agent's diff, not the score its own container gives its own
    leftovers — so the diff has to leave the pod as a file. Harbor has no hook
    for "after the agent, before the verifier", so the seam is here: every
    agent this package publishes wraps ``run()`` and snapshots the tree on the
    way out.

    ``finally``, not "on success". The rollouts worth re-grading include the
    ones that ran out of budget with a complete fix already written — on the
    2026-09-21 V1 sweep half of all solved trials were rollouts that had timed
    out — so a capture that skipped the error path would drop exactly the
    trials it exists for.

    The capture never propagates a failure. It is bookkeeping that runs after
    the work is done, and an exception here would turn a finished rollout into
    a failed one.
    """

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        try:
            await super().run(instruction, environment, context)  # type: ignore[misc]
        finally:
            await self._capture_model_patch(environment)

    async def _capture_model_patch(self, environment: BaseEnvironment) -> None:
        """Snapshot the working tree to :data:`MODEL_PATCH_PATH`, best effort."""
        try:
            result = await self.exec_as_root(  # type: ignore[attr-defined]
                environment,
                command=model_patch_routine(MODEL_PATCH_PATH),
                timeout_sec=MODEL_PATCH_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            self.logger.warning(  # type: ignore[attr-defined]
                "could not capture %s, so this trial cannot be re-graded from "
                "its patch: %s",
                MODEL_PATCH_PATH,
                exc,
            )
            return
        # The routine reports the byte count on stdout. Zero is not an error
        # here — an agent that changed nothing is a real outcome — but it is
        # the first thing to look at when a re-grade scores zero, so it is
        # logged rather than left in the exec's output alone.
        stdout = (getattr(result, "stdout", "") or "").strip()
        if stdout:
            self.logger.info("%s", stdout.splitlines()[-1])  # type: ignore[attr-defined]


class _PayloadBackedAgent:
    """Shared ``install()`` policy. Mixed in ahead of Harbor's agent class."""

    #: Name of the wrapper under ``$TOOLS_DIR/bin``.
    payload_tool: str
    #: A flag the CLI accepts and exits 0 from.
    payload_probe_flag: str = "--version"
    #: From ``BaseInstalledAgent``: the version a caller pinned, if any.
    _version: str | None

    @property
    def _payload_binary(self) -> str:
        return f"{TOOLS_DIR}/bin/{self.payload_tool}"

    async def _payload_runs(self, environment: BaseEnvironment) -> bool:
        result = await environment.exec(
            command=_probe(self._payload_binary, self.payload_probe_flag)
        )
        return result.return_code == 0

    async def _is_musl(self, environment: BaseEnvironment) -> bool:
        result = await environment.exec(command=IS_MUSL)
        return result.return_code == 0

    async def install(self, environment: BaseEnvironment) -> None:
        if await self._payload_runs(environment):
            if self._version is None:
                self.logger.debug(
                    "%s is already usable at %s; skipping Harbor's installer",
                    self.payload_tool,
                    self._payload_binary,
                )
                await self._after_payload_install(environment)
                return
            # A pinned version is a request only Harbor's installer can honour:
            # the payload ships whatever the tools image baked in, and silently
            # running that instead would answer a different question than the
            # one asked. On a musl image this then fails the way it always did.
            self.logger.info(
                "%s is available from the pod, but version %s was pinned, so "
                "Harbor's installer runs anyway",
                self.payload_tool,
                self._version,
            )
        elif await self._is_musl(environment):
            raise PayloadUnusableError(
                f"this task's image is musl-based (Alpine), where neither route "
                f"to {self.payload_tool!r} works. {self._payload_binary} is a "
                "glibc ELF, so it resolves on PATH and then fails to execve; "
                "and Harbor's own installer for this agent needs glibc too "
                "(nvm serves no musl Node build, and uv falls back to sdists "
                "the image cannot compile). Rebuild the sandbox-tools image "
                "with a musl-capable payload, or exclude these tasks — "
                "configs/swebench-pro-musl.txt lists the 88 in SWE-bench Pro."
            )

        await super().install(environment)  # type: ignore[misc]

    async def _after_payload_install(self, environment: BaseEnvironment) -> None:
        """Anything the agent needs once the payload has been adopted.

        Adopting the payload is also the moment an image is known to be one the
        payload had to be carried into — in practice a musl one — so this is
        where the rest of the POSIX gaps get filled.
        """
        await self._write_shim(environment, "stdbuf", STDBUF_SHIM, only_if_missing=True)

    async def _write_shim(
        self,
        environment: BaseEnvironment,
        name: str,
        script: str,
        *,
        only_if_missing: bool = False,
    ) -> None:
        """Put *script* on PATH as ``/usr/local/bin/<name>``.

        A shim rather than a symlink, matching how the orchestrator publishes
        the tools payload, so this does not depend on ``ln`` behaving the same
        on busybox. ``only_if_missing`` never shadows a real implementation the
        image already provides.
        """
        path = f"/usr/local/bin/{name}"
        write = (
            f"mkdir -p /usr/local/bin && "
            f"printf %s {shlex.quote(script)} > {shlex.quote(path)} && "
            f"chmod 0755 {shlex.quote(path)}"
        )
        if only_if_missing:
            write = f"command -v {shlex.quote(name)} >/dev/null 2>&1 || {{ {write}; }}"
        await self.exec_as_root(environment, command=write)


class Pi(_CleanTreeGate, _ModelPatchCapture, _PayloadBackedAgent, _Pi):
    """``pi`` from the payload, or Harbor's nvm install on a glibc image.

    Harbor invokes the agent as ``. ~/.nvm/nvm.sh; pi ... | stdbuf -oL tee``.
    The leading ``.`` failing with no nvm present is harmless — ``_exec`` sets
    ``-o pipefail`` and not ``-e`` — and ``pi`` resolves from PATH. The
    ``stdbuf`` at the end of that pipeline is not harmless on busybox, which is
    why the base class shims it.
    """

    payload_tool = "pi"

    #: Harbor's run command invokes the payload's own name, so unlike
    #: mini-swe-agent there is nothing to bridge — but the shim is still
    #: written, to put the deadline wrapper in front of it.
    RUN_COMMAND_NAME = "pi"

    async def _after_payload_install(self, environment: BaseEnvironment) -> None:
        """Republish the payload's ``pi`` with a deadline around it.

        The orchestrator appends its tools directory to PATH and never
        prepends it, so ``/usr/local/bin`` wins: this shadows the shim the
        orchestrator wrote there and delegates to the same absolute path it
        would have run. Only reached once the payload has been adopted, so the
        binary is not in question.

        One behaviour change worth naming: the orchestrator writes its shim
        only when the image has no ``pi`` of its own, so on an image that ships
        one, Harbor's bare ``pi`` would until now have found the image's. This
        shim shadows that too, which matches what this class already decided in
        ``install()`` but is not what happened before. No task image in the
        suites run here ships a ``pi``.
        """
        await super()._after_payload_install(environment)
        await self._write_shim(
            environment,
            self.RUN_COMMAND_NAME,
            deadline_shim(
                self._payload_binary,
                deadline_var=DEADLINE_ENV_VAR,
                grace=AGENT_KILL_GRACE_S,
            ),
        )


class MiniSweAgent(
    _CleanTreeGate, _ModelPatchCapture, _PayloadBackedAgent, _MiniSweAgent
):
    """``mini`` from the payload, or Harbor's ``uv tool install`` otherwise.

    One difference from what Harbor would have installed: the payload carries
    ``mini-swe-agent`` and its own dependencies, but not the ``orjson`` and
    ``fastapi`` that Harbor requests alongside litellm. They are accelerators on
    litellm's tool-calling path rather than requirements, and the same payload
    already drives ``orchard-eval run``'s mini harness — but if a trial ever
    fails importing one of them, adding them to the ``minitools`` stage of
    ``orchard_env/Dockerfile.tools`` is the fix, not reinstating the uv install.
    """

    payload_tool = "mini"
    #: mini is a typer app with no --version; --help runs the same imports,
    #: which is what a broken interpreter or a missing dependency shows up in.
    payload_probe_flag = "--help"

    #: Seconds one bash command the agent runs may take before mini kills it.
    #: The ``mini`` config Harbor loads allows 30, which a Go build or a jest
    #: run in a SWE-bench Pro image can need more than: on the V2 HARD-51 tasks
    #: 1.8% of command outputs reported a timeout, against 0.26% for a
    #: standalone mini-swe-agent setup allowing 600s.
    DEFAULT_STEP_TIMEOUT_S = 120

    def __init__(
        self,
        *args,
        step_timeout: int | None = DEFAULT_STEP_TIMEOUT_S,
        temperature: float | None = None,
        config: dict | None = None,
        config_file: str | None = None,
        **kwargs,
    ) -> None:
        """Harbor's agent, with mini's per-command timeout and temperature set.

        Both land in the config Harbor layers over ``-c mini``:

        ``step_timeout`` (``--ak step_timeout=N``)
            ``environment.timeout``; ``0`` or ``None`` keeps mini's own.
        ``temperature`` (``--ak temperature=T``)
            ``model.model_kwargs.temperature``, which litellm sends with every
            request. ``None`` sends none, so the server's default applies.

        A ``config`` that sets either itself wins. A ``config_file`` is passed
        through untouched — Harbor cannot merge one — so asking for a
        temperature alongside it is an error rather than silently dropped.
        """
        if config_file is not None:
            if temperature is not None:
                raise ValueError(
                    "temperature cannot be combined with config_file; set "
                    "model.model_kwargs.temperature in the file instead"
                )
        else:
            config = dict(config or {})
            if step_timeout:
                environment = dict(config.get("environment") or {})
                environment.setdefault("timeout", int(step_timeout))
                config["environment"] = environment
            if temperature is not None:
                model = dict(config.get("model") or {})
                model_kwargs = dict(model.get("model_kwargs") or {})
                model_kwargs.setdefault("temperature", float(temperature))
                model["model_kwargs"] = model_kwargs
                config["model"] = model
            config = config or None
        super().__init__(*args, config=config, config_file=config_file, **kwargs)

    #: What Harbor's run command invokes. The payload publishes the wrapper
    #: under mini's other console-script name, so the two have to be bridged.
    RUN_COMMAND_NAME = "mini-swe-agent"

    async def _after_payload_install(self, environment: BaseEnvironment) -> None:
        """Publish the payload's ``mini`` under the name Harbor's run uses.

        ``MiniSweAgent.run()`` shells out to ``mini-swe-agent``; the payload's
        wrapper is ``mini``. Written unconditionally, unlike the shared shims:
        a ``mini-swe-agent`` already on PATH is one Harbor's uv install left
        behind, and the payload is the one this class chose.
        """
        await super()._after_payload_install(environment)
        await self._write_shim(
            environment,
            self.RUN_COMMAND_NAME,
            deadline_shim(
                self._payload_binary,
                deadline_var=DEADLINE_ENV_VAR,
                grace=AGENT_KILL_GRACE_S,
            ),
        )

    def get_version_command(self) -> str | None:
        """Read the version off the payload before falling back to uv.

        Upstream asks uv which tools it installed, which reports nothing once
        the uv install is skipped — so every result would carry a blank version.
        ``mini`` itself has no ``--version`` flag, but the tools image records
        what it baked in, in the shape Harbor's ``parse_version`` already
        expects (``mini    2.4.6``).
        """
        upstream = super().get_version_command()
        payload = f"grep -m1 '^{self.payload_tool} ' {TOOLS_DIR}/VERSIONS 2>/dev/null"
        return f"{payload} || {{ {upstream}; }}" if upstream else payload


class ClaudeCode(_CleanTreeGate, _ModelPatchCapture, _PayloadBackedAgent, _ClaudeCode):
    """``claude`` from the payload, or Harbor's bootstrap on a glibc image.

    Harbor's own installer already short-circuits when ``claude`` resolves, so
    unlike ``pi`` this class is not what makes the payload get used. What it
    adds is the distinction that short-circuit cannot draw: Harbor's check is
    ``command -v claude``, a PATH lookup, and the payload's ``claude`` is a
    glibc ELF. On one of the 88 musl images in SWE-bench Pro it resolves, so
    Harbor concludes the agent is installed, and the trial then dies on a loader
    error attributed to the agent. Probing with ``--version`` instead — which is
    what :class:`_PayloadBackedAgent` does — turns that into the stated reason.

    Harbor's fallback needs the network either way (``npm install -g`` or the
    bootstrap script from downloads.claude.ai), so on an isolated pod the
    payload is the only route that works at all.
    """

    payload_tool = "claude"

    #: Harbor's run command invokes ``claude`` by name, after prepending
    #: ``$HOME/.local/bin`` to PATH. Nothing to bridge, but the shim still gets
    #: written to put the deadline wrapper in front of it.
    RUN_COMMAND_NAME = "claude"

    async def _after_payload_install(self, environment: BaseEnvironment) -> None:
        """Republish the payload's ``claude`` with a deadline around it.

        Same reasoning as :meth:`Pi._after_payload_install`: a timeout cancels
        only the harness-side await, so without this the CLI keeps running —
        and keeps billing the fleet — long after Harbor has stopped listening.
        """
        await super()._after_payload_install(environment)
        await self._write_shim(
            environment,
            self.RUN_COMMAND_NAME,
            deadline_shim(
                self._payload_binary,
                deadline_var=DEADLINE_ENV_VAR,
                grace=AGENT_KILL_GRACE_S,
            ),
        )


class Codex(_CleanTreeGate, _ModelPatchCapture, _Codex):
    """Harbor's ``codex``, plus the ``model.patch`` capture. Nothing else.

    Deliberately *not* a :class:`_PayloadBackedAgent`. As the module docstring
    says, codex needs no help getting installed: Harbor's own agent
    short-circuits when ``codex`` is on PATH, and the payload's codex is a
    static-PIE musl build that runs on every image here. Subclassing it to
    change ``install()`` would be reintroducing a problem that does not exist.

    What it cannot do is write the patch, and the re-grade pass scores a trial
    zero without one — so this exists purely to put :class:`_ModelPatchCapture`
    in front of it. ``name()`` and every other behaviour come from Harbor
    unchanged, so results keep reading ``codex``, and the notes in
    ``scripts/run_swebench_pro_harbor.sh`` about Harbor's codex defaults
    (``reasoning_effort=high``, ``unified_exec``) still describe what runs.

    ``ORCHARD_HARBOR_STOCK_AGENTS=1`` opts back out, as for the other three.
    """
