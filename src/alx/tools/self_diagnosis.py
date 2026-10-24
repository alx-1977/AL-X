"""AL/X reads her own execution trace and runtime log (D-050).

Two read-only primitives. They report what happened; deciding what it means,
and whether and how to put it right, is hers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    SideEffect,
    StructuredSchema,
    ValueKind,
)

READ_OWN_TRACE = "read_own_trace"
READ_OWN_LOG = "read_own_log"
_MAX = 500
_DEFAULT_WINDOW = timedelta(hours=24)
_FAILURES = ("arguments_unusable", "unavailable")

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
_OBJECTS = StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.OBJECT))
_RESULT = StructuredSchema(
    ValueKind.OBJECT, {"entries": _OBJECTS, "truncated": _BOOLEAN},
    ("entries", "truncated"), extra_properties=False)

TRACE_DEFINITION = CapabilityDefinition(
    READ_OWN_TRACE,
    "Read your own execution trace: every step you took, as shown on Friedl's screen "
    "(subsystem, status, label, capability or goal reference, reason code, duration). "
    "Each step's time is local, with its offset. Also returns reasoning_usage: "
    "per-call input/output/cache tokens and cost_usd, plus full-window totals; "
    "null means unknown, not zero. Usage is for the time window across conversations, "
    "independent of trace filters. "
    "A usage read failure is reported in reasoning_usage.failure; trace entries remain available. "
    "Window from/to, ISO 8601 (a time without an offset is local); default the "
    "last 24 hours, kept for 30 days. "
    "problems_only returns only failed and refused steps; subsystem narrows to one "
    "(core, mail, notebook, research, readers, xero, coding, review ...). Up to 500, "
    "oldest first; truncated says when more exist.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"from": _STRING, "to": _STRING, "problems_only": _BOOLEAN,
         "subsystem": _STRING, "limit": _INTEGER},
        (), extra_properties=False),
    StructuredSchema(
        ValueKind.OBJECT,
        {"entries": _OBJECTS, "truncated": _BOOLEAN,
         "reasoning_usage": StructuredSchema(ValueKind.OBJECT)},
        ("entries", "truncated"), extra_properties=False),
    SideEffect.NONE,
    _FAILURES,
)

LOG_DEFINITION = CapabilityDefinition(
    READ_OWN_LOG,
    "Read your own runtime log: what this process did and any warnings, errors and "
    "tracebacks, including launcher output before logging starts. Logs are kept "
    "for 30 days. Window from/to uses ISO 8601; a time without an offset is local. "
    "The default window is the last 24 hours. Untimestamped process output uses "
    "the file modification time, labelled timestamp_source=file_mtime. "
    "Process output is shown at ERROR level. "
    "minimum_level is INFO, WARNING or ERROR (default "
    "WARNING); contains keeps entries with that text. Anything shaped like a key is "
    "masked. Up to 500 entries, the newest kept; truncated says when more matched.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"from": _STRING, "to": _STRING, "minimum_level": _STRING,
         "contains": _STRING, "limit": _INTEGER},
        (), extra_properties=False),
    _RESULT,
    SideEffect.NONE,
    _FAILURES,
)

DEFINITIONS = (TRACE_DEFINITION, LOG_DEFINITION)


def _window(arguments: Mapping[str, Any], now: datetime) -> tuple[datetime, datetime, int]:
    def moment(name: str) -> datetime | None:
        value = arguments.get(name)
        if value in (None, ""):
            return None
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.astimezone()

    end = moment("to") or now
    start = moment("from") or (end - _DEFAULT_WINDOW)
    if end <= start:
        raise ValueError("to must be later than from")
    limit = int(arguments.get("limit", _MAX))
    if not 0 < limit <= _MAX:
        raise ValueError(f"limit must be 1 to {_MAX}")
    return start, end, limit


def build_self_diagnosis_executors(
    trace: Any,
    read_log: Callable[..., tuple[list[dict], bool]],
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
    read_usage: Callable[..., dict[str, Any]] | None = None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def failed(name: str, code: str, reason: str = "") -> CapabilityResult:
        failure = {"code": code}
        if reason:
            failure["reason"] = reason[:300]
        return CapabilityResult(call_id_source(), name, CapabilityResultState.FAILED,
                                failure=failure)

    def read_trace(arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            start, end, limit = _window(arguments, now())
            subsystem = str(arguments.get("subsystem") or "").strip().lower()
            problems = bool(arguments.get("problems_only", False))
        except (TypeError, ValueError) as error:
            return failed(READ_OWN_TRACE, "arguments_unusable", str(error))
        try:
            entries, truncated = trace.between(start, end, problems, subsystem, limit)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return failed(READ_OWN_TRACE, "unavailable", type(error).__name__)
        values = {"entries": entries, "truncated": truncated}
        if read_usage is not None:
            try:
                usage = read_usage(start, end, limit)
            except Exception as error:  # noqa: BLE001 - preserve the successful trace read
                usage = {"failure": {"code": "unavailable", "reason": type(error).__name__}}
            if usage is not None:
                values["reasoning_usage"] = usage
        return CapabilityResult(call_id_source(), READ_OWN_TRACE, CapabilityResultState.SUCCEEDED,
                                values)

    def read_own_log(arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            start, end, limit = _window(arguments, now())
            level = str(arguments.get("minimum_level") or "WARNING").upper()
            if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                raise ValueError("minimum_level must be INFO, WARNING or ERROR")
            contains = str(arguments.get("contains") or "")
        except (TypeError, ValueError) as error:
            return failed(READ_OWN_LOG, "arguments_unusable", str(error))
        try:
            entries, truncated = read_log(start, end, level, contains, limit)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return failed(READ_OWN_LOG, "unavailable", type(error).__name__)
        return CapabilityResult(call_id_source(), READ_OWN_LOG, CapabilityResultState.SUCCEEDED,
                                {"entries": entries, "truncated": truncated})

    return {READ_OWN_TRACE: read_trace, READ_OWN_LOG: read_own_log}


__all__ = ["DEFINITIONS", "READ_OWN_LOG", "READ_OWN_TRACE", "build_self_diagnosis_executors"]
