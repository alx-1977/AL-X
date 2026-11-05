"""AL/X reads her own past conversations through the authoritative store.

One read-only primitive. It returns retained turns as stored; what they mean,
and what to do about them, is hers. The search text is matched literally and
never interpreted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState, ContentOrigin,
    RetentionPolicy, SideEffect, StructuredSchema, ValueKind,
)

READ_OWN_CONVERSATIONS = "read_own_conversations"
_MAX = 50
_DEFAULT_LIMIT = 20
_DEFAULT_WINDOW = timedelta(days=7)

_STRING = StructuredSchema(ValueKind.STRING)
DEFINITION = CapabilityDefinition(
    READ_OWN_CONVERSATIONS,
    "Read your own past conversations across every thread: what Friedl said and what you "
    "replied, including replies from background work and mail threads. Each turn gives "
    "conversation_id, turn_id, origin (typed, speech_transcript or alx_response), its "
    "local time with offset, and its text. query is literal case-insensitive text "
    "search over what was said; conversation_id narrows to one thread. Window from/to, "
    "ISO 8601 (a time without an offset is local); default the last 7 days. Newest "
    f"first, limit defaults to {_DEFAULT_LIMIT}, maximum {_MAX}; truncated says when "
    "more matched.",
    StructuredSchema(ValueKind.OBJECT, {
        "query": _STRING, "conversation_id": _STRING, "from": _STRING, "to": _STRING,
        "limit": StructuredSchema(ValueKind.INTEGER),
    }, (), extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, {
        "turns": StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.OBJECT)),
        "truncated": StructuredSchema(ValueKind.BOOLEAN),
    }, ("turns", "truncated"), extra_properties=False),
    SideEffect.NONE,
    ("arguments_unusable", "unavailable"),
    # The search text can quote what was said, so a goal's audit keeps the
    # window and thread only, as it keeps only the ids of the turns read.
    durable_input_fields=("conversation_id", "from", "to", "limit"),
)


def _moment(arguments: Mapping[str, Any], name: str) -> datetime | None:
    value = arguments.get(name)
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.astimezone()


def _text(arguments: Mapping[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def build_conversation_history_executors(
    store: Any,
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def read(arguments: Mapping[str, Any]) -> CapabilityResult:
        def failed(code: str, reason: str = "") -> CapabilityResult:
            failure = {"code": code}
            if reason:
                failure["reason"] = reason[:300]
            return CapabilityResult(call_id_source(), READ_OWN_CONVERSATIONS,
                                    CapabilityResultState.FAILED, failure=failure)

        if not DEFINITION.input_schema.accepts(arguments):
            return failed("arguments_unusable")
        moment = now()
        try:
            end = _moment(arguments, "to") or moment
            start = _moment(arguments, "from") or (end - _DEFAULT_WINDOW)
            if end <= start:
                raise ValueError("to must be later than from")
            limit = arguments.get("limit", _DEFAULT_LIMIT)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= _MAX:
                raise ValueError(f"limit must be 1 to {_MAX}")
            query = _text(arguments, "query")
            conversation_id = _text(arguments, "conversation_id")
        except (TypeError, ValueError) as error:
            return failed("arguments_unusable", str(error))
        try:
            turns, truncated = store.search_turns(
                contains=query, conversation_id=conversation_id,
                start=start, end=end, limit=limit, at=moment,
            )
        except Exception as error:  # noqa: BLE001 - storage failure is evidence for the Core
            return failed("unavailable", type(error).__name__)
        values = {
            "turns": [{
                "conversation_id": turn.conversation_id,
                "turn_id": turn.turn_id,
                "origin": turn.origin.value,
                "at": turn.occurred_at.astimezone().isoformat(timespec="seconds"),
                "text": turn.content,
            } for turn in turns],
            "truncated": truncated,
        }
        sources = [turn.provenance for turn in turns if turn.provenance is not None]
        provenance = (None if not sources else
                      RetentionPolicy().derive(ContentOrigin.ALX, moment, sources))
        return CapabilityResult(call_id_source(), READ_OWN_CONVERSATIONS,
                                CapabilityResultState.SUCCEEDED, values,
                                # The words stay in the conversation store; the
                                # goal's audit keeps only which turns were read.
                                durable_values={
                                    "turn_ids": [turn.turn_id for turn in turns],
                                    "truncated": truncated,
                                },
                                provenance=provenance)

    return {READ_OWN_CONVERSATIONS: read}


__all__ = ["DEFINITION", "READ_OWN_CONVERSATIONS", "build_conversation_history_executors"]
