"""One reusable shipment tracking capability; no conversational decisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState,
    ContentOrigin, RetentionPolicy, SideEffect, StructuredSchema, ValueKind,
)


TRACK_SHIPMENT = "track_shipment"
_STRING = StructuredSchema(ValueKind.STRING)
_EVENT = StructuredSchema(
    ValueKind.OBJECT,
    {"time": _STRING, "location": _STRING, "description": _STRING,
     "status": _STRING, "courier_code": _STRING},
    extra_properties=False,
)
_FAILURES = (
    "arguments_unusable", "not_configured", "authentication_failed",
    "rate_limited", "tracking_not_found", "tracking_ambiguous",
    "provider_failed", "connection_failed",
)

DEFINITION = CapabilityDefinition(
    TRACK_SHIPMENT,
    "Return Ship24's current shipment status and full event history for one "
    "tracking number, with an optional Ship24 courier code. The first call "
    "may create a tracker and consume shipment quota. Reports Ship24's facts "
    "without deciding what they mean.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"tracking_number": _STRING, "courier_code": _STRING},
        ("tracking_number",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"tracking_number": _STRING, "courier": _STRING,
         "status": _STRING, "status_milestone": _STRING,
         "estimated_delivery": _STRING, "last_update": _STRING,
         "events": StructuredSchema(ValueKind.ARRAY, items=_EVENT)},
        ("tracking_number", "status_milestone", "events"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)


def build_ship24_executors(
    provider: Any,
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def track(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()

        def failed(code: str) -> CapabilityResult:
            return CapabilityResult(
                call_id, TRACK_SHIPMENT, CapabilityResultState.FAILED,
                failure={"code": code},
            )

        number = arguments.get("tracking_number")
        courier = arguments.get("courier_code")
        if (not isinstance(number, str) or not number.strip()
                or (courier is not None and (
                    not isinstance(courier, str) or not courier.strip()
                ))):
            return failed("arguments_unusable")
        try:
            result = provider.track(number, courier)
        except Exception as error:
            code = getattr(error, "code", None)
            return failed(code if code in _FAILURES else "provider_failed")
        at = datetime.now(UTC)
        return CapabilityResult(
            call_id, TRACK_SHIPMENT, CapabilityResultState.SUCCEEDED,
            result,
            provenance=RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, at),
        )

    return {TRACK_SHIPMENT: track}
