"""D-045: AL/X's full access to Friedl's Particle account.

`particle_api_request` is any Particle Cloud API call: devices, products,
firmware and over-the-air updates, webhooks, SIMs, usage and the rest. Which
call to make, and whether it is wise, is AL/X's judgement; Friedl granted
the whole account ("FULL access … She will be managing it anyway"). The
token only ever goes to Particle's API address.

`read_particle_usage` reads data operations per device per day from
Particle's usage report, which needs a request, a wait and a download from
Particle's storage.

Every change made (any call other than GET) and every usage report is
recorded in the reader log, so it can be accounted for afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ContentOrigin,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.provenance import RetentionPolicy
from alx.contracts.readers import ReaderAccessError

PARTICLE_API_REQUEST = "particle_api_request"
READ_PARTICLE_USAGE = "read_particle_usage"

_STRING = StructuredSchema(ValueKind.STRING)
_OBJECT = StructuredSchema(ValueKind.OBJECT)
_ANY = StructuredSchema(ValueKind.ANY)

_FAILURES = (
    "arguments_unusable",
    "connection_failed",
    "device_timeout",
    "event_stream_not_supported",
    "usage_unavailable",
    "usage_not_ready",
    "usage_unreadable",
)

API_DEFINITION = CapabilityDefinition(
    PARTICLE_API_REQUEST,
    "Make any Particle Cloud API call on Friedl's account, with full authority (D-045): method GET, POST, PUT, PATCH or DELETE; path a /v1/ path on api.particle.io (for example /v1/products/45984/devices, /v1/products/45984/devices/<id>/<function> to call a function, /v1/products/45984/firmware, /v1/user/sims, /v1/products/45984/integrations); optional query parameters and JSON body. Returns Particle's HTTP status and reply; a refusal is returned, not hidden. Event streams are not available here. Changes (anything but GET) are recorded in the reader log.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"method": _STRING, "path": _STRING, "query": _OBJECT, "body": _ANY},
        ("method", "path"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"status": StructuredSchema(ValueKind.INTEGER), "body": _ANY},
        ("status", "body"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

USAGE_DEFINITION = CapabilityDefinition(
    READ_PARTICLE_USAGE,
    "Read Particle data operations per device per day for a date range (start and end, YYYY-MM-DD, inclusive; a day is only complete once it has ended, UTC), optionally for given device IDs, from Particle's usage report. Returns rows (date, device_id, device_name, product, connectivity, data_operations, firmware_version, device_os) and the total. Particle also emails Friedl a copy of the report.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"start": _STRING, "end": _STRING,
         "devices": StructuredSchema(ValueKind.ARRAY, items=_STRING)},
        ("start", "end"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"rows": StructuredSchema(ValueKind.ARRAY, items=_OBJECT),
         "total_data_operations": StructuredSchema(ValueKind.INTEGER)},
        ("rows", "total_data_operations"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

DEFINITIONS = (API_DEFINITION, USAGE_DEFINITION)


def build_particle_executors(
    particle: Any,
    log: Callable[[datetime, str, str, Mapping[str, Any]], None],
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def failed(capability: str, code: str) -> CapabilityResult:
        return CapabilityResult(call_id_source(), capability, CapabilityResultState.FAILED,
                                failure={"code": code})

    def api_request(arguments: StructuredData) -> CapabilityResult:
        method = str(arguments.get("method") or "").upper()
        path = arguments.get("path")
        query = arguments.get("query")
        if query is not None and not isinstance(query, Mapping):
            return failed(PARTICLE_API_REQUEST, "arguments_unusable")
        try:
            status, body = particle.api(method, path, dict(query or {}), arguments.get("body"))
        except ReaderAccessError as error:
            return failed(PARTICLE_API_REQUEST, error.code)
        at = now()
        if method != "GET":
            # Whose device a change concerns is in the path; the log keeps
            # the call itself, not its body, which can be large.
            log(at, "particle", "particle_api",
                {"method": method, "path": path, "status": status})
        return CapabilityResult(
            call_id_source(), PARTICLE_API_REQUEST, CapabilityResultState.SUCCEEDED,
            {"status": status, "body": body},
            provenance=RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, at),
        )

    def read_usage(arguments: StructuredData) -> CapabilityResult:
        devices = arguments.get("devices") or ()
        if not isinstance(devices, (list, tuple)) or any(not isinstance(d, str) for d in devices):
            return failed(READ_PARTICLE_USAGE, "arguments_unusable")
        start, end = arguments.get("start"), arguments.get("end")
        try:
            rows = particle.usage(start, end, tuple(devices))
        except ReaderAccessError as error:
            return failed(READ_PARTICLE_USAGE, error.code)
        at = now()
        total = sum(row["data_operations"] for row in rows)
        log(at, "particle", "particle_usage_report",
            {"start": start, "end": end, "devices": len({r["device_id"] for r in rows}),
             "total_data_operations": total})
        return CapabilityResult(
            call_id_source(), READ_PARTICLE_USAGE, CapabilityResultState.SUCCEEDED,
            {"rows": rows, "total_data_operations": total},
            provenance=RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, at),
        )

    return {PARTICLE_API_REQUEST: api_request, READ_PARTICLE_USAGE: read_usage}
