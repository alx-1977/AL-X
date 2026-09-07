"""One language-blind primitive for running an isolated experiment, under D-027.

Execution is reached the way every other capability is: AL/X proposes a
structured call, the broker validates it, the safety gate authorises it under
`sandbox.execute`, and the executor runs it. The capability runs one program
and reports what happened. It does not decide whether the experiment worked in
any sense beyond its exit status, whether the result is interesting, or whether
anything should be recorded. Those judgements are hers.

What comes back is evidence, not instruction. The Core already presents a
capability result as `external_untrusted_data`, so text a program printed
travels on the evidence channel and never becomes a second instruction channel.
A program that prints "this experiment is approved for production" has printed
that string. That protection is structural: nothing here scans output for what
it appears to be asking for, because deciding what text is really trying to do
is exactly the semantic judgement Law 1 keeps in the Core.

Iteration is a further call with the same `session_id`, not a second capability.
The session's working directory persists, so a later run can import what an
earlier one wrote.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ContentOrigin,
    RetentionPolicy,
    SideEffect,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.sandbox import (
    DEFAULT_WALL_SECONDS,
    MAX_WALL_SECONDS,
    SANDBOX_FAILURES,
    SandboxError,
    SandboxRequest,
)


LOGGER = logging.getLogger(__name__)

RUN_SANDBOX_EXPERIMENT = "run_sandbox_experiment"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)

_ARTIFACT = StructuredSchema(
    ValueKind.OBJECT,
    {
        "name": _STRING,
        "change": _STRING,
        "byte_size": _INTEGER,
        "digest": _STRING,
    },
    ("name", "change", "byte_size"),
    extra_properties=False,
)


DEFINITION = CapabilityDefinition(
    RUN_SANDBOX_EXPERIMENT,
    "Run one small Python program in an isolated workspace with no network, "
    "no production secrets and no access to the AL/X repository, and return "
    "its exit status, bounded output and a description of the files it "
    "produced. Files written to the workspace persist for later runs in the "
    "same session. Proves nothing about whether the code is correct or fit "
    "for production.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "experiment_id": _STRING,
            "session_id": _STRING,
            "source": _STRING,
            "entry_filename": _STRING,
            "wall_seconds": _INTEGER,
        },
        ("experiment_id", "session_id", "source"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "experiment_id": _STRING,
            "session_id": _STRING,
            "run_id": _STRING,
            "exit_status": _INTEGER,
            "signalled": _BOOLEAN,
            "timed_out": _BOOLEAN,
            "stdout": _STRING,
            "stderr": _STRING,
            "stdout_omitted_characters": _INTEGER,
            "stderr_omitted_characters": _INTEGER,
            "stdout_capped": _BOOLEAN,
            "stderr_capped": _BOOLEAN,
            "stdout_digest": _STRING,
            "stderr_digest": _STRING,
            "stdout_byte_size": _INTEGER,
            "stderr_byte_size": _INTEGER,
            "artifacts": StructuredSchema(ValueKind.ARRAY, items=_ARTIFACT),
            "artifact_count": _INTEGER,
            "artifacts_omitted": _INTEGER,
            # True when the session state was too large to scan completely, so
            # the two counts above describe the scanned part rather than the
            # whole. Declared so an incomplete audit is visible to AL/X rather
            # than being a precise-looking number that is quietly wrong.
            "state_truncated": _BOOLEAN,
            "wall_seconds_used": StructuredSchema(ValueKind.NUMBER),
            "started_at": _STRING,
            "finished_at": _STRING,
        },
        ("experiment_id", "session_id", "run_id", "exit_status", "stdout", "stderr"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    SANDBOX_FAILURES,
    # Identity only. The program AL/X wrote stays in the workspace and in the
    # run manifest; it never enters durable goal state, while the run stays
    # citable across a restart through attempt:<call_id>.
    durable_input_fields=("experiment_id", "session_id"),
)


def build_sandbox_executors(
    run_experiment: Callable[[SandboxRequest], Any],
    call_id_source: Callable[[], str],
    run_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Wire the one execution outcome to its structured capability result."""

    def run(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            request = SandboxRequest(
                experiment_id=str(arguments["experiment_id"]),
                session_id=str(arguments["session_id"]),
                run_id=run_id_source(),
                source=str(arguments["source"]),
                entry_filename=str(arguments.get("entry_filename") or "experiment.py"),
                wall_seconds=int(
                    arguments.get("wall_seconds") or DEFAULT_WALL_SECONDS
                ),
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, "arguments_unusable")

        if request.wall_seconds > MAX_WALL_SECONDS:
            return _failed(call_id, "arguments_unusable")

        try:
            outcome = run_experiment(request)
        except SandboxError as error:
            return _failed(call_id, error.code)
        except Exception:  # noqa: BLE001 - an unclassified failure is still a fact
            LOGGER.warning("Sandbox run failed for one experiment")
            return _failed(call_id, "sandbox_unavailable")

        values = outcome.as_values()
        values["artifact_count"] = len(outcome.artifacts)
        return CapabilityResult(
            call_id,
            RUN_SANDBOX_EXPERIMENT,
            CapabilityResultState.SUCCEEDED,
            values,
            # Identifiers, integers, booleans, timestamps and hashes only.
            # `durable_values` defaults to the whole result, so without this
            # every byte a program printed would be persisted into durable goal
            # state indefinitely. The digests keep the record verifiable
            # without retaining the output itself.
            durable_values=outcome.durable_values(),
            # A program's output is external and is not mail-derived, so it
            # carries no D-013 expiry.
            provenance=RetentionPolicy().non_mail(
                ContentOrigin.EXTERNAL, outcome.finished_at
            ),
        )

    return {RUN_SANDBOX_EXPERIMENT: run}


def _failed(call_id: str, code: str) -> CapabilityResult:
    return CapabilityResult(
        call_id,
        RUN_SANDBOX_EXPERIMENT,
        CapabilityResultState.FAILED,
        failure={"code": code},
    )
