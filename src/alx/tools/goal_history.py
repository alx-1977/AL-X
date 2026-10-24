"""Read AL/X's own goal history through her existing authoritative goal store."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState,
    DurableGoalStore, RetentionPolicy, ContentOrigin,
    SideEffect, StructuredSchema, ValueKind,
)

READ_OWN_GOALS = "read_own_goals"
DEFINITION = CapabilityDefinition(
    READ_OWN_GOALS,
    "Search your own retained goals across all conversations, finished or unfinished. "
    "Provide query for literal case-insensitive text search over stored content "
    "(tracking numbers, names, invoice numbers, context, progress, evidence). "
    "Results give goal ids, objectives and statuses, newest written first; limit "
    "defaults to 10, maximum 50. Or provide goal_id (exact or unambiguous prefix) "
    "alone to read that goal's stored state with the same bounded attempt result "
    "summaries; ambiguous prefixes return arguments_unusable. "
    "Exactly one of query and goal_id is required.",
    StructuredSchema(ValueKind.OBJECT, {
        "query": StructuredSchema(ValueKind.STRING),
        "goal_id": StructuredSchema(ValueKind.STRING),
        "limit": StructuredSchema(ValueKind.INTEGER),
    }, extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, {
        "goals": StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.OBJECT)),
        "truncated": StructuredSchema(ValueKind.BOOLEAN),
    }, ("goals", "truncated"), extra_properties=False),
    SideEffect.NONE,
    ("arguments_unusable", "goal_not_found", "unavailable"),
    trace_fields=("goal_id",),
)


def build_goal_history_executors(
    store: DurableGoalStore,
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def read(arguments: Mapping[str, Any]) -> CapabilityResult:
        def failed(code: str) -> CapabilityResult:
            return CapabilityResult(call_id_source(), READ_OWN_GOALS,
                                    CapabilityResultState.FAILED, failure={"code": code})

        if not DEFINITION.input_schema.accepts(arguments):
            return failed("arguments_unusable")
        try:
            values, sources = store.read_history(
                contains=arguments.get("query"), goal_id=arguments.get("goal_id"),
                limit=arguments.get("limit", 10),
            )
        except (TypeError, ValueError):
            return failed("arguments_unusable")
        except Exception:  # storage failure is evidence for the Core
            return failed("unavailable")
        if "goal_id" in arguments and not values["goals"]:
            return failed("goal_not_found")
        provenance = (None if not sources else
                      RetentionPolicy().derive(ContentOrigin.ALX, now(), sources))
        return CapabilityResult(call_id_source(), READ_OWN_GOALS,
                                CapabilityResultState.SUCCEEDED, values,
                                # Keep identities in the audit, not recursive
                                # copies of goals inside other goal attempts.
                                durable_values={
                                    "goal_ids": [item["goal_id"] for item in values["goals"]],
                                    "truncated": values["truncated"],
                                },
                                provenance=provenance)

    return {READ_OWN_GOALS: read}
