"""Hand one brief of mechanical work to the worker (D-070)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState, SideEffect,
    StructuredSchema, ValueKind,
)
from alx.specialists.worker import MechanicalWorker, WorkerError

DELEGATE_MECHANICAL_WORK = "delegate_mechanical_work"
DEFAULT_CALLS = 40
MAX_CALLS = 80

# D-070: the capabilities the worker may call. Checks, analyses and edits on
# her own copies, and reads; nothing that sends, spends, merges, writes
# Friedl's files or changes her own attention. run_program is not here: it
# runs any command as Friedl's user, which no fixed limit here can bound
# (review of #207).
WORKER_CAPABILITIES = (
    "check_kicad_board",
    "edit_kicad_work_copy",
    "read_reference_file",
    "analyse_kicad_copper",
    "simulate_kicad_circuit",
    "compile_particle_firmware",
    "read_own_goals",
    "read_mail_document",
    "read_mail_attachment",
)

_STRING = StructuredSchema(ValueKind.STRING)
_OBJECTS = StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.OBJECT))

DEFINITION = CapabilityDefinition(
    DELEGATE_MECHANICAL_WORK,
    "For many-step mechanical work (board checks, work-copy edits, analyses, simulations), "
    "hand one brief to your worker, a cheaper model with its own small context, and get one "
    "checked report back. Keep judgement for yourself. It cannot run general programs. Write the brief as you would for a careful technician: what to run or measure, "
    "on which of your copies, what counts as a checked result, and when to stop. The worker "
    "can call only " + ", ".join(WORKER_CAPABILITIES) + ", through the same safety gate as "
    "you, with no approvals: it cannot send, spend, merge or change Friedl's files. It "
    "reports status (done, stopped or blocked), results each with how it was checked, open "
    "points it could not settle, and files it touched; calls lists every call it made and "
    "its outcome. The report is evidence for you to check, not a conclusion. "
    f"max_calls defaults to {DEFAULT_CALLS}, at most {MAX_CALLS}; a run that reaches it, "
    "or 20 minutes, stops with stop_reason and no report; a report that arrives after 20 "
    "minutes is kept with stop_reason time_limit. A run that fails returns worker_failed "
    "with the calls it completed and what each returned. One brief runs at a time.",
    StructuredSchema(ValueKind.OBJECT, {
        "brief": _STRING,
        "max_calls": StructuredSchema(ValueKind.INTEGER),
    }, ("brief",), extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, {
        "status": _STRING,
        "stop_reason": _STRING,
        "report": StructuredSchema(ValueKind.OBJECT),
        "calls": _OBJECTS,
    }, ("status", "calls"), extra_properties=False),
    # It edits her work copies through the worker.
    SideEffect.EFFECTFUL,
    ("arguments_unusable", "worker_busy", "worker_failed"),
)


def build_worker_executors(
    worker: MechanicalWorker, call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def delegate(arguments: Mapping[str, Any]) -> CapabilityResult:
        # Taken before the worker's own calls rebind the current call.
        call_id = call_id_source()

        def failed(code: str, calls: Any = None) -> CapabilityResult:
            failure: dict[str, Any] = {"code": code}
            if calls:
                # What the worker completed before it failed, so she can see
                # it before deciding what to do next.
                failure["calls"] = calls
            return CapabilityResult(call_id, DELEGATE_MECHANICAL_WORK,
                                    CapabilityResultState.FAILED, failure=failure)

        brief = arguments.get("brief")
        max_calls = arguments.get("max_calls", DEFAULT_CALLS)
        if (not isinstance(brief, str) or not brief.strip() or isinstance(max_calls, bool)
                or not isinstance(max_calls, int) or not 0 < max_calls <= MAX_CALLS):
            return failed("arguments_unusable")
        try:
            outcome = worker.run(brief, call_id, max_calls)
        except WorkerError as error:
            return failed("worker_busy" if error.code == "worker_busy" else "worker_failed",
                          error.calls)
        except Exception:  # noqa: BLE001 - a provider failure is evidence for the Core
            return failed("worker_failed")
        values = {"status": outcome["status"], "calls": outcome["calls"]}
        if outcome.get("stop_reason"):
            values["stop_reason"] = outcome["stop_reason"]
        if outcome.get("report") is not None:
            values["report"] = outcome["report"]
        return CapabilityResult(call_id, DELEGATE_MECHANICAL_WORK,
                                CapabilityResultState.SUCCEEDED, values)

    return {DELEGATE_MECHANICAL_WORK: delegate}


__all__ = ["DEFINITION", "DELEGATE_MECHANICAL_WORK", "WORKER_CAPABILITIES",
           "build_worker_executors"]
