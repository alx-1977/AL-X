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
    DuplicateFutureCognition,
    FutureCognitionNotFound,
    FutureCognitionRequest,
    FutureCognitionTooSoon,
)

REQUEST_FUTURE_COGNITION = "request_future_cognition"
WITHDRAW_FUTURE_COGNITION = "withdraw_future_cognition"

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

DEFINITIONS = (REQUEST_DEFINITION, WITHDRAW_DEFINITION)


def _failed(call_id: str, capability_id: str, code: str) -> CapabilityResult:
    return CapabilityResult(
        call_id, capability_id, CapabilityResultState.FAILED, failure={"code": code}
    )


def _code(error: Exception) -> str:
    return {
        "DuplicateFutureCognition": "request_already_exists",
        "FutureCognitionNotFound": "request_not_found",
        "FutureCognitionTooSoon": "requested_time_too_soon",
    }.get(type(error).__name__, "storage_failed")


def build_continuity_executors(
    store: Any,
    retention_days: int,
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Bind both primitives to the one durable continuity store."""
    now_of = clock or (lambda: datetime.now(UTC))

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

    return {
        REQUEST_FUTURE_COGNITION: request,
        WITHDRAW_FUTURE_COGNITION: withdraw,
    }
