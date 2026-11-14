"""Hand one brief of mechanical work to the worker (D-070)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from alx.contracts import (
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition,
    CapabilityResult, CapabilityResultState, SideEffect, StructuredSchema, ValueKind,
)
from alx.contracts.capabilities import ArgumentAlternatives, arguments_match
from alx.specialists.worker import MechanicalWorker, WorkerError
from alx.tools.mail import MAIL, MailAction

DELEGATE_MECHANICAL_WORK = "delegate_mechanical_work"
DEFAULT_CALLS = 40
MAX_CALLS = 80

# D-070: the capabilities the worker may call. Checks, analyses and edits on
# her own copies, and reads; nothing that sends, spends, merges, writes
# Friedl's files or changes her own attention. run_program is not here: it
# runs any command as Friedl's user, which no fixed limit here can bound
# (review of #207).
# look reads her own page images (datasheet drawings); it changes nothing
# (14 Nov: the worker could not read a land-pattern drawing without it).
WORKER_CAPABILITIES = (
    "kicad",
    "look",
    "compile_particle_firmware",
    "read_own_goals",
    "read_mail_document",
    MAIL,
)
# D-074: a capability of which the worker may use only some actions, and the
# exact argument alternatives it may call. Mail needs no approval, so holding
# no approvals would not keep the worker from searching, acknowledging, marking,
# flagging, filing or trashing; it may only read an attachment.
WORKER_LIMITS: tuple[tuple[str, ArgumentAlternatives], ...] = (
    (MAIL, ((("action", MailAction.READ_ATTACHMENT.value),),)),
)


def worker_call_refusal(call: CapabilityCall) -> CapabilityAttempt | None:
    """A refusal for a call outside the worker's limit, or None when it may go on.

    Exact structured values only. A refused call never reaches the broker, so
    nothing is invoked; the worker sees it as rejected, as any gate refusal.
    """
    for capability_id, allowed in WORKER_LIMITS:
        if call.capability_id == capability_id and not arguments_match(allowed, call.arguments):
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False,
                                     reason_code="worker_action_not_allowed")
    return None


def _limits() -> str:
    return " ".join(
        f"Of {capability_id} it may use only "
        + " or ".join(", ".join(f"{key} {value}" for key, value in alternative)
                      for alternative in allowed) + "."
        for capability_id, allowed in WORKER_LIMITS)

_STRING = StructuredSchema(ValueKind.STRING)
_OBJECTS = StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.OBJECT))

DEFINITION = CapabilityDefinition(
    DELEGATE_MECHANICAL_WORK,
    "For many-step mechanical work (board checks, work-copy edits, analyses, simulations), "
    "hand one brief to your worker, a cheaper model with its own small context, and get one "
    "checked report back. Keep judgement for yourself. It cannot run general programs. Write the brief as you would for a careful technician: what to run or measure, "
    "on which of your copies, what counts as a checked result, and when to stop. The worker "
    "can call only " + ", ".join(WORKER_CAPABILITIES) + ". " + _limits() + " It calls them through the same safety gate as "
    "you, with no approvals: it cannot send, spend, merge or change Friedl's files. It "
    "reports status (done, stopped or blocked), results each with how it was checked, open "
    "points it could not settle, and files it touched; calls lists every call it made and "
    "its outcome. The report is evidence for you to check, not a conclusion. For a decision "
    "that matters (a part choice, an RF layout, a change to his design) ask it for an "
    "independent check: give it the question and where the evidence is, never your answer, "
    "and compare its result with yours before you tell him. "
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


__all__ = ["DEFINITION", "DELEGATE_MECHANICAL_WORK", "WORKER_CAPABILITIES", "WORKER_LIMITS",
           "build_worker_executors", "worker_call_refusal"]
