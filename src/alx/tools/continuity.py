"""Two language-blind primitives: ask for a later occasion, or withdraw one.

`note` passes through this module untouched. It is read from the structured
arguments, handed to storage, and returned in the result exactly as written.
Nothing here parses it, matches it, measures it or branches on it, and no
failure code depends on what it says. That is the whole discipline of this
file: AL/X writes to her future self, and deterministic code is the courier.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

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
from alx.contracts.continuity import (
    CarriedThought,
    CarriedThoughtNotFound,
    DuplicateCarriedThought,
    DuplicateFutureCognition,
    FutureCognitionNotFound,
    FutureCognitionRequest,
    FutureCognitionTooSoon,
)

REQUEST_FUTURE_COGNITION = "request_future_cognition"
WITHDRAW_FUTURE_COGNITION = "withdraw_future_cognition"
RECORD_CARRIED_THOUGHT = "record_carried_thought"
WITHDRAW_CARRIED_THOUGHT = "withdraw_carried_thought"
MARK_CARRIED_THOUGHT_RAISED = "mark_carried_thought_raised"
RESOLVE_UNDELIVERED_RESPONSE = "resolve_undelivered_response"

# How many open thoughts one reasoning turn is shown. A fixed uniform bound, so
# a long list is trimmed by count rather than by anyone deciding which of her
# thoughts is worth seeing.
OPEN_THOUGHT_LIMIT = 20

# A self-requested occasion may not be immediate. This is the mechanical
# anti-tight-loop bound from D-024: it stops a turn spawning a turn without
# wall-clock time passing. It is not a judgement about the request, and it is
# not a quota — there is no limit on how many she may make.
MINIMUM_HORIZON_SECONDS = 60

_STRING = StructuredSchema(ValueKind.STRING)
_STRINGS = StructuredSchema(ValueKind.ARRAY, items=_STRING)

_FAILURES = (
    "arguments_unusable",
    "request_not_found",
    "thought_not_found",
    "thought_already_exists",
    "occasion_not_found",
    "request_already_exists",
    "requested_time_too_soon",
    "storage_failed",
)

REQUEST_DEFINITION = CapabilityDefinition(
    REQUEST_FUTURE_COGNITION,
    "Ask for another cognition opportunity no earlier than a given time, "
    "carrying a private note to your future self. The note is stored and "
    "returned to you unread; nothing interprets it.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "request_id": _STRING,
            "not_before": _STRING,
            "note": _STRING,
            "references": _STRINGS,
        },
        ("request_id", "not_before", "note"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "request_id": _STRING,
            "not_before": _STRING,
            "status": _STRING,
        },
        ("request_id", "not_before", "status"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

WITHDRAW_DEFINITION = CapabilityDefinition(
    WITHDRAW_FUTURE_COGNITION,
    "Withdraw one future cognition request you no longer want, by its exact "
    "identity.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"request_id": _STRING},
        ("request_id",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"request_id": _STRING, "status": _STRING},
        ("request_id", "status"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

def _failed(call_id: str, capability_id: str, code: str) -> CapabilityResult:
    return CapabilityResult(
        call_id, capability_id, CapabilityResultState.FAILED, failure={"code": code}
    )


def _code(error: Exception) -> str:
    return {
        "DuplicateFutureCognition": "request_already_exists",
        "FutureCognitionNotFound": "request_not_found",
        "FutureCognitionTooSoon": "requested_time_too_soon",
        "DuplicateCarriedThought": "thought_already_exists",
        "CarriedThoughtNotFound": "thought_not_found",
    }.get(type(error).__name__, "storage_failed")


_THOUGHT = StructuredSchema(
    ValueKind.OBJECT,
    {
        "thought_id": _STRING,
        "content": _STRING,
        "formed_at": _STRING,
        "status": _STRING,
    },
    ("thought_id", "content", "formed_at", "status"),
    extra_properties=False,
)

RECORD_THOUGHT_DEFINITION = CapabilityDefinition(
    RECORD_CARRIED_THOUGHT,
    "Keep a thought on your mind in your own words. It is not a goal, a "
    "notebook entry, a memory or a request for another occasion; it is "
    "something unfinished you want to hold. Nothing acts on it by itself.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"thought_id": _STRING, "content": _STRING, "references": _STRINGS},
        ("thought_id", "content"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"thought_id": _STRING, "status": _STRING},
        ("thought_id", "status"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

WITHDRAW_THOUGHT_DEFINITION = CapabilityDefinition(
    WITHDRAW_CARRIED_THOUGHT,
    "Let go of a thought you no longer hold, by its exact identity.",
    StructuredSchema(
        ValueKind.OBJECT, {"thought_id": _STRING}, ("thought_id",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"thought_id": _STRING, "status": _STRING},
        ("thought_id", "status"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

MARK_RAISED_DEFINITION = CapabilityDefinition(
    MARK_CARRIED_THOUGHT_RAISED,
    "Record that you have brought a thought into conversation. Only you can "
    "say that you raised it.",
    StructuredSchema(
        ValueKind.OBJECT, {"thought_id": _STRING}, ("thought_id",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"thought_id": _STRING, "status": _STRING},
        ("thought_id", "status"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)


RESOLVE_UNDELIVERED_DEFINITION = CapabilityDefinition(
    RESOLVE_UNDELIVERED_RESPONSE,
    "Close an autonomous occasion whose response never reached Friedl, once "
    "you have decided what to do about it. Say something now and resolve it, "
    "or resolve it because it no longer matters. Nothing resolves it for you.",
    StructuredSchema(
        ValueKind.OBJECT, {"opportunity_id": _STRING}, ("opportunity_id",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT, {"opportunity_id": _STRING}, ("opportunity_id",),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

DEFINITIONS = (
    REQUEST_DEFINITION,
    WITHDRAW_DEFINITION,
    RECORD_THOUGHT_DEFINITION,
    WITHDRAW_THOUGHT_DEFINITION,
    MARK_RAISED_DEFINITION,
    RESOLVE_UNDELIVERED_DEFINITION,
)


def build_continuity_executors(
    store: Any,
    retention_days: int,
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
    conversation_id_source: Callable[[], str] | None = None,
    occasions: Any = None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Bind both primitives to the one durable continuity store."""
    now_of = clock or (lambda: datetime.now(UTC))
    # Deterministic, from the executing turn. Never a capability argument: the
    # schema has no conversation field, so a request cannot name a thread it
    # did not arise in.
    conversation_of = conversation_id_source or (lambda: "")

    def request(values: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            now = now_of()
            not_before = datetime.fromisoformat(str(values["not_before"]))
            if not_before.tzinfo is None or not_before.utcoffset() is None:
                return _failed(call_id, REQUEST_FUTURE_COGNITION, "arguments_unusable")
            # The only check applied to a request, and it is about the clock.
            if not_before < now + timedelta(seconds=MINIMUM_HORIZON_SECONDS):
                raise FutureCognitionTooSoon(MINIMUM_HORIZON_SECONDS)
            proposal = FutureCognitionRequest(
                request_id=str(values["request_id"]),
                not_before=not_before,
                # Carried verbatim. Never inspected.
                note=str(values["note"]),
                requested_at=now,
                conversation_id=conversation_of(),
                references=tuple(
                    str(item) for item in values.get("references", ()) or ()
                ),
                provenance=RetentionPolicy().non_mail(ContentOrigin.ALX, now),
            )
            stored = store.create(proposal)
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, REQUEST_FUTURE_COGNITION, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, REQUEST_FUTURE_COGNITION, _code(error))
        return CapabilityResult(
            call_id,
            REQUEST_FUTURE_COGNITION,
            CapabilityResultState.SUCCEEDED,
            {
                "request_id": stored.request_id,
                "not_before": stored.not_before.isoformat(),
                "status": stored.status.value,
            },
            # The receipt names the request; it does not repeat her note back
            # into goal state, where it would become durable prose nobody asked
            # to keep there.
            durable_values={"request_id": stored.request_id},
        )

    def withdraw(values: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            stored = store.withdraw(str(values["request_id"]))
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, WITHDRAW_FUTURE_COGNITION, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, WITHDRAW_FUTURE_COGNITION, _code(error))
        return CapabilityResult(
            call_id,
            WITHDRAW_FUTURE_COGNITION,
            CapabilityResultState.SUCCEEDED,
            {"request_id": stored.request_id, "status": stored.status.value},
            durable_values={"request_id": stored.request_id},
        )

    def record_thought(values: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            now = now_of()
            thought = CarriedThought(
                thought_id=str(values["thought_id"]),
                # Carried verbatim. Never inspected.
                content=str(values["content"]),
                formed_at=now,
                references=tuple(
                    str(item) for item in values.get("references", ()) or ()
                ),
                provenance=RetentionPolicy().non_mail(ContentOrigin.ALX, now),
            )
            stored = store.record_thought(thought)
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, RECORD_CARRIED_THOUGHT, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, RECORD_CARRIED_THOUGHT, _code(error))
        return CapabilityResult(
            call_id,
            RECORD_CARRIED_THOUGHT,
            CapabilityResultState.SUCCEEDED,
            {"thought_id": stored.thought_id, "status": stored.status.value},
            durable_values={"thought_id": stored.thought_id},
        )

    def _transition(
        capability_id: str, operation: Callable[[str], Any]
    ) -> Callable[[Mapping[str, Any]], CapabilityResult]:
        def run(values: Mapping[str, Any]) -> CapabilityResult:
            call_id = call_id_source()
            try:
                stored = operation(str(values["thought_id"]))
            except (KeyError, TypeError, ValueError):
                return _failed(call_id, capability_id, "arguments_unusable")
            except Exception as error:
                return _failed(call_id, capability_id, _code(error))
            return CapabilityResult(
                call_id,
                capability_id,
                CapabilityResultState.SUCCEEDED,
                {"thought_id": stored.thought_id, "status": stored.status.value},
                durable_values={"thought_id": stored.thought_id},
            )

        return run

    def resolve_undelivered(values: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            opportunity_id = str(values["opportunity_id"])
            if occasions is None:
                return _failed(
                    call_id, RESOLVE_UNDELIVERED_RESPONSE, "occasion_not_found"
                )
            if not occasions.resolve_undelivered(opportunity_id):
                return _failed(
                    call_id, RESOLVE_UNDELIVERED_RESPONSE, "occasion_not_found"
                )
        except (KeyError, TypeError, ValueError):
            return _failed(
                call_id, RESOLVE_UNDELIVERED_RESPONSE, "arguments_unusable"
            )
        except Exception:
            return _failed(call_id, RESOLVE_UNDELIVERED_RESPONSE, "storage_failed")
        return CapabilityResult(
            call_id,
            RESOLVE_UNDELIVERED_RESPONSE,
            CapabilityResultState.SUCCEEDED,
            {"opportunity_id": opportunity_id},
            durable_values={"opportunity_id": opportunity_id},
        )

    return {
        RESOLVE_UNDELIVERED_RESPONSE: resolve_undelivered,
        REQUEST_FUTURE_COGNITION: request,
        WITHDRAW_FUTURE_COGNITION: withdraw,
        RECORD_CARRIED_THOUGHT: record_thought,
        WITHDRAW_CARRIED_THOUGHT: _transition(
            WITHDRAW_CARRIED_THOUGHT, store.withdraw_thought
        ),
        MARK_CARRIED_THOUGHT_RAISED: _transition(
            MARK_CARRIED_THOUGHT_RAISED, store.mark_thought_raised
        ),
    }
