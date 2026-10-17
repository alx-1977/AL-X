"""AL/X restarts herself onto code she has merged (D-055).

Merged code is not live until the process restarts. Friedl, 2026-10-17: AL/X
should be able to improve things around her Core, and an improvement she has
merged should go live without him. One primitive: restart onto committed
main. Whether now is a good moment is hers; the two conditions under which a
restart is impossible or harmful are checked here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    SideEffect,
    StructuredSchema,
    ValueKind,
)

RESTART_SELF = "restart_self"
_FAILURES = ("nothing_new_to_run", "coding_job_running", "unavailable")

_STRING = StructuredSchema(ValueKind.STRING)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)

DEFINITION = CapabilityDefinition(
    RESTART_SELF,
    "Restart yourself onto committed main, so code you have merged goes live. "
    "Refused with nothing_new_to_run when you already run main's commit, and "
    "with coding_job_running while a coding job is running. The restart starts "
    "a few seconds after this returns and takes about half a minute; Friedl's "
    "page reconnects by itself, and your goals, plans and conversations carry "
    "on from durable state.",
    StructuredSchema(ValueKind.OBJECT, {}, (), extra_properties=False),
    StructuredSchema(
        ValueKind.OBJECT,
        {"restart_scheduled": _BOOLEAN, "running_commit": _STRING, "main_commit": _STRING},
        ("restart_scheduled", "running_commit", "main_commit"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)


def build_self_restart_executors(
    running_commit: Callable[[], str],
    main_commit: Callable[[], str],
    coding_job_running: Callable[[], bool],
    restart: Callable[[], None],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def failed(code: str, **details: object) -> CapabilityResult:
        return CapabilityResult(call_id_source(), RESTART_SELF, CapabilityResultState.FAILED,
                                failure={"code": code, **details})

    def restart_self(_arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            running, main = running_commit(), main_commit()
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return failed("unavailable", reason=type(error).__name__)
        if not running or not main:
            return failed("unavailable", reason="commit_unknown")
        if running == main:
            return failed("nothing_new_to_run", running_commit=running)
        if coding_job_running():
            # A restart would stop the job's session mid-edit. Its checkpoint
            # survives, but nothing is gained by forcing it.
            return failed("coding_job_running")
        try:
            restart()
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return failed("unavailable", reason=type(error).__name__)
        return CapabilityResult(
            call_id_source(), RESTART_SELF, CapabilityResultState.SUCCEEDED,
            {"restart_scheduled": True, "running_commit": running, "main_commit": main},
        )

    return {RESTART_SELF: restart_self}


__all__ = ["DEFINITION", "RESTART_SELF", "build_self_restart_executors"]
