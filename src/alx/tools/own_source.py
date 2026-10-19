"""One structured, read-only source inspection capability (D-057)."""

from collections.abc import Callable, Mapping
from typing import Any

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState,
    SideEffect, StructuredSchema, ValueKind,
)

READ_OWN_SOURCE = "read_own_source"
_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
DEFINITION = CapabilityDefinition(
    READ_OWN_SOURCE,
    "Inspect your source at runtime running_commit, independently of the checkout. "
    "operation search takes nonempty literal query, optional ignore_case (false), "
    "path_prefix (file or directory), limit (default 50, maximum 200). "
    "operation read takes repository-relative path, optional inclusive start_line "
    "(default 1) and end_line. Returns UTF-8 text only; symlinks and credential-shaped "
    "paths are refused. Output is bounded to 32000 characters; truncated also marks "
    "searches skipping files over 1 MiB. Oversized reads fail source_too_large. "
    "Missing running-commit objects fail unavailable; never falls back to HEAD.",
    StructuredSchema(ValueKind.OBJECT, {
        "operation": _STRING, "query": _STRING, "ignore_case": _BOOLEAN,
        "path_prefix": _STRING, "limit": _INTEGER, "path": _STRING,
        "start_line": _INTEGER, "end_line": _INTEGER,
    }, ("operation",), extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, {
        "running_commit": _STRING, "truncated": _BOOLEAN,
        "path": _STRING, "content": _STRING, "start_line": _INTEGER,
        "total_lines": _INTEGER,
        "matches": StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(
            ValueKind.OBJECT, {"path": _STRING, "line_number": _INTEGER, "line_text": _STRING},
            ("path", "line_number", "line_text"), extra_properties=False)),
    }, ("running_commit", "truncated"), extra_properties=False),
    SideEffect.NONE,
    ("arguments_unusable", "path_denied", "file_not_found", "unsupported_file",
     "binary_file", "source_too_large", "unavailable"),
    trace_fields=("operation",),
)


def build_own_source_executors(
    inspect: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def read_own_source(arguments: Mapping[str, Any]) -> CapabilityResult:
        code = "arguments_unusable"
        if DEFINITION.input_schema.accepts(arguments):
            try:
                values = inspect(arguments)
                return CapabilityResult(call_id_source(), READ_OWN_SOURCE,
                                        CapabilityResultState.SUCCEEDED, values)
            except Exception as error:
                # Safe technical codes only; Git stderr and exception messages
                # never become result content.
                code = str(error) if str(error) in DEFINITION.possible_failure_codes else "unavailable"
        return CapabilityResult(call_id_source(), READ_OWN_SOURCE,
                                CapabilityResultState.FAILED, failure={"code": code})

    return {READ_OWN_SOURCE: read_own_source}
