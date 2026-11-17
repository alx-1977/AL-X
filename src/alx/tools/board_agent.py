"""Hand a whole KiCad board job to the board agent, and pass on Friedl's answers (D-083)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from alx.contracts import (
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, SideEffect, StructuredSchema, ValueKind,
)
from alx.contracts.capabilities import arguments_match
from alx.specialists.board_agent import BoardJobAgent
from alx.specialists.worker import WorkerError
from alx.tools.kicad import KICAD, KiCadAction

DELEGATE_BOARD_JOB = "delegate_board_job"
DEFAULT_CALLS = 80
MAX_CALLS = 200

# D-083: the board agent's tools. KiCad work on her copies and looking at its
# own drawings. Every kicad action that changes nothing of his is allowed,
# except opening windows on his screen, which is hers to do with him.
BOARD_AGENT_CAPABILITIES = (KICAD, "look")
BOARD_AGENT_KICAD_ACTIONS = tuple(item.value for item in (
    KiCadAction.INSPECT, KiCadAction.CHECK, KiCadAction.EDIT, KiCadAction.READ_REFERENCE,
    KiCadAction.ANALYSE_COPPER, KiCadAction.SIMULATE, KiCadAction.CHECK_LAYOUT,
    KiCadAction.MAKE_HIERARCHICAL, KiCadAction.FETCH_DATASHEET, KiCadAction.RENDER,
    KiCadAction.INSPECT_SCHEMATIC, KiCadAction.TAKE_EDITS))


def board_agent_call_refusal(call: CapabilityCall) -> CapabilityAttempt | None:
    """A refusal for a kicad action outside the agent's list, or None when it may go on."""
    allowed = tuple((("action", name),) for name in BOARD_AGENT_KICAD_ACTIONS)
    if call.capability_id == KICAD and not arguments_match(allowed, call.arguments):
        return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False,
                                 reason_code="board_agent_action_not_allowed")
    # D-084: the agent changes a copy only through a structured change.
    if call.capability_id == KICAD and call.arguments.get("script") is not None:
        return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False,
                                 reason_code="board_agent_script_not_allowed")
    return None


_STRING = StructuredSchema(ValueKind.STRING)

DEFINITION = CapabilityDefinition(
    DELEGATE_BOARD_JOB,
    "Hand a whole KiCad board job (an edit of one of his boards, or a new board) to your board "
    "agent, which does it making the layout choices itself, in big steps, in his order: "
    "schematic, placement, routing (D-082), following his preferences (ALX_Storage/Preferences), "
    "which it is always given. Run it as a planned step so you stay free for him. brief starts a "
    "job: what he asked in his words, the project, what done means for this step in a sentence "
    "(from docs/BOARD_EDIT_DONE.md, which the agent cannot read) and anything he has already decided. Each stretch of its work ends with a report: the end of a "
    "step (step_ready), a question it cannot settle from his drawings or datasheets (question), "
    "the end (done) or a dead end (blocked); report names the work copy and the revision. Then "
    "show him that step with kicad open_in_kicad (his original beside it only when he wants to compare), look at the screenshot, and tell him plainly; "
    "record his yes with kicad approve_step; and pass on what he said with job_id and message, in "
    "his words: the next stretch starts from the job's journal. stop true with job_id stops a "
    "running stretch at once (stopped says whether one was running); only that stops it. It "
    "cannot approve steps, open windows on his screen or write back: those stay with you. Its "
    "calls go through the same gate as yours, with no approvals. calls lists every call it made. "
    "max_calls bounds each stretch (default %d, at most %d), as do 30 minutes. One job runs at a "
    "time." % (DEFAULT_CALLS, MAX_CALLS),
    StructuredSchema(ValueKind.OBJECT, {
        "brief": _STRING,
        "job_id": _STRING,
        "message": _STRING,
        "max_calls": StructuredSchema(ValueKind.INTEGER),
        "stop": StructuredSchema(ValueKind.BOOLEAN),
    }, (), extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, {
        "job_id": _STRING,
        "status": _STRING,
        "stop_reason": _STRING,
        "report": StructuredSchema(ValueKind.OBJECT),
        "calls": StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.OBJECT)),
        "stopped": StructuredSchema(ValueKind.BOOLEAN),
    }, ("job_id", "status"), extra_properties=False),
    # It edits her work copies through the agent.
    SideEffect.EFFECTFUL,
    ("arguments_unusable", "worker_busy", "worker_failed", "job_unknown"),
)


def build_board_agent_executors(
    agent: BoardJobAgent, call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def delegate(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()

        def failed(code: str, calls: Any = None) -> CapabilityResult:
            failure: dict[str, Any] = {"code": code}
            if calls:
                failure["calls"] = calls
            return CapabilityResult(call_id, DELEGATE_BOARD_JOB, CapabilityResultState.FAILED, failure=failure)

        brief, job_id, message = arguments.get("brief"), arguments.get("job_id"), arguments.get("message")
        if arguments.get("stop") is True:
            # Never waits for the running stretch: it is the way to end one.
            if not isinstance(job_id, str) or brief is not None or message is not None:
                return failed("arguments_unusable")
            stopped = agent.stop(job_id)
            return CapabilityResult(call_id, DELEGATE_BOARD_JOB, CapabilityResultState.SUCCEEDED,
                                    {"job_id": job_id, "status": "stopping" if stopped else "not_running",
                                     "stopped": stopped})
        max_calls = arguments.get("max_calls", DEFAULT_CALLS)
        starting = isinstance(brief, str) and bool(brief.strip()) and job_id is None and message is None
        resuming = (brief is None and isinstance(job_id, str) and bool(job_id.strip())
                    and isinstance(message, str) and bool(message.strip()))
        if (not (starting or resuming) or isinstance(max_calls, bool) or not isinstance(max_calls, int)
                or not 0 < max_calls <= MAX_CALLS):
            return failed("arguments_unusable")
        try:
            outcome = agent.work(job_id=job_id if resuming else None, brief=brief if starting else None,
                                 message=message if resuming else None, max_calls=max_calls)
        except WorkerError as error:
            code = error.code if error.code in ("worker_busy", "job_unknown") else "worker_failed"
            return failed(code, error.calls)
        except Exception:  # noqa: BLE001 - a provider failure is evidence for the Core
            return failed("worker_failed")
        return CapabilityResult(call_id, DELEGATE_BOARD_JOB, CapabilityResultState.SUCCEEDED,
                                {key: value for key, value in outcome.items() if value is not None})

    return {DELEGATE_BOARD_JOB: delegate}


__all__ = ["BOARD_AGENT_CAPABILITIES", "BOARD_AGENT_KICAD_ACTIONS", "DEFINITION", "DELEGATE_BOARD_JOB",
           "board_agent_call_refusal", "build_board_agent_executors"]
