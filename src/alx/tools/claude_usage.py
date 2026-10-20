"""Argument-free observation of AL/X's own subscription limits."""

from collections.abc import Callable, Mapping
from typing import Any

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState,
    SideEffect, StructuredSchema, ValueKind,
)

READ_CLAUDE_USAGE = 'read_claude_usage'
_STRING = StructuredSchema(ValueKind.STRING)
DEFINITION = CapabilityDefinition(
    READ_CLAUDE_USAGE,
    "Read your own Claude subscription limits through the existing Claude login. "
    "Returns printed labels, percent used and reset text, raw screen text and read time. "
    "Unreadable or timed-out screens return failure evidence, never estimates.",
    StructuredSchema(ValueKind.OBJECT, extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, {
        'limits': StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(
            ValueKind.OBJECT, {
                'label': _STRING, 'percent_used': StructuredSchema(ValueKind.NUMBER),
                'resets_at': _STRING,
            }, ('label', 'percent_used', 'resets_at'), extra_properties=False)),
        'screen_capture': _STRING, 'read_at': _STRING,
    }, ('limits', 'screen_capture', 'read_at'), extra_properties=False),
    SideEffect.NONE,
    ('cli_unavailable', 'usage_timeout', 'usage_unreadable'),
)


def build_claude_usage_executors(
    read: Callable[[], Mapping[str, Any]], call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def read_usage(arguments: Mapping[str, Any]) -> CapabilityResult:
        values = dict(read())
        code = values.pop('failure_code', None)
        return CapabilityResult(
            call_id_source(), READ_CLAUDE_USAGE,
            CapabilityResultState.FAILED if code else CapabilityResultState.SUCCEEDED,
            values, failure={'code': code} if code else None,
        )

    return {READ_CLAUDE_USAGE: read_usage}
