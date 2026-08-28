"""Translate between durable AL/X state and a replaceable reasoning model."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from alx.contracts import (
    AgentDecision,
    CapabilityCall,
    Evidence,
    GoalState,
    GoalStatus,
    GoalStopReason,
    ModelMessage,
    ModelRequest,
    ModelRole,
    MemoryKind,
    MemoryProposal,
    MemoryQuery,
    MemorySourceMatch,
    Objective,
    ProgressRecord,
    ReasoningContext,
    ReasoningModel,
    Referent,
    StructuredSchema,
    SuccessCriterion,
    WorkItem,
)


PROTOCOL_INSTRUCTIONS = """You are the single authoritative AL/X reasoning Core.
Interpret the complete durable goal and conversation context. Choose either one
authoritative response or one reusable primitive capability call. Evaluate prior
results, update the goal honestly, and continue toward it while safe useful work
remains. Never route by phrase, call an unregistered capability, fabricate evidence, erase
history, alter approvals, or claim completion without evidence supporting every
success criterion. You may optionally form memories through semantic judgement;
never create them by score, keyword, quota, or schedule. Every memory source must
use an available durable reference exactly as supplied. Return only the required
structured decision."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _object_json(value: str, field_name: str) -> Mapping[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must encode an object")
    return parsed


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field_name} must contain non-blank strings")
    return tuple(value)


def _records(value: Any, record_type: type, field_name: str) -> tuple[Any, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{field_name} must be an array")
    records = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field_name} entries must be objects")
        if record_type is ProgressRecord:
            records.append(
                ProgressRecord(
                    item["id"], item["summary"], _strings(item["evidence_refs"], "evidence_refs")
                )
            )
        elif record_type is WorkItem:
            records.append(WorkItem(item["id"], item["summary"]))
        elif record_type is SuccessCriterion:
            records.append(SuccessCriterion(item["id"], item["description"]))
        elif record_type is Referent:
            records.append(
                Referent(item["id"], _object_json(item["attributes_json"], "referent attributes"))
            )
        elif record_type is Evidence:
            records.append(
                Evidence(
                    item["id"],
                    item["kind"],
                    _object_json(item["attributes_json"], "evidence attributes"),
                    _strings(item["supports"], "evidence supports"),
                )
            )
        else:
            raise TypeError("unsupported record type")
    return tuple(records)


def _without_reused_ids(
    existing: tuple[Any, ...],
    proposed: tuple[Any, ...],
    attribute: str,
    field_name: str,
) -> tuple[Any, ...]:
    existing_ids = {getattr(item, attribute) for item in existing}
    proposed_ids = [getattr(item, attribute) for item in proposed]
    if len(proposed_ids) != len(set(proposed_ids)) or existing_ids.intersection(proposed_ids):
        raise ValueError(f"{field_name} cannot reuse durable record identifiers")
    return proposed


def _state_payload(state: GoalState) -> dict[str, Any]:
    return {
        "goal_id": state.goal_id,
        "objective": {
            "source_reference": state.objective.source_reference,
            "summary": state.objective.summary,
        },
        "success_criteria": [
            {"id": item.criterion_id, "description": item.description}
            for item in state.success_criteria
        ],
        "context": _plain(state.context),
        "referents": [
            {"id": item.referent_id, "attributes": _plain(item.attributes)}
            for item in state.referents
        ],
        "decisions": [
            {"id": item.record_id, "summary": item.summary, "evidence_refs": list(item.evidence_refs)}
            for item in state.decisions
        ],
        "corrections": [
            {"id": item.record_id, "summary": item.summary, "evidence_refs": list(item.evidence_refs)}
            for item in state.corrections
        ],
        "progress": [
            {"id": item.record_id, "summary": item.summary, "evidence_refs": list(item.evidence_refs)}
            for item in state.progress
        ],
        "attempts": [
            {
                "call_id": None if item.call is None else item.call.call_id,
                "capability_id": None if item.call is None else item.call.capability_id,
                "disposition": item.disposition.value,
                "result_state": None if item.result is None else item.result.state.value,
                "result_values": None if item.result is None else _plain(item.result.values),
                "failure": None if item.result is None or item.result.failure is None else _plain(item.result.failure),
            }
            for item in state.attempts
        ],
        "blockers": [{"id": item.item_id, "summary": item.summary} for item in state.blockers],
        "outstanding_work": [
            {"id": item.item_id, "summary": item.summary} for item in state.outstanding_work
        ],
        "evidence": [
            {
                "id": item.evidence_id,
                "kind": item.kind,
                "attributes": _plain(item.attributes),
                "supports": list(item.supports),
            }
            for item in state.evidence
        ],
        "approvals": [
            {
                "id": item.approval_id,
                "capability_id": item.scope.capability_id,
                "arguments": _plain(item.scope.arguments),
                "lifecycle": item.lifecycle.value,
            }
            for item in state.approvals
        ],
        "status": state.status.value,
        "stop_reason": None if state.stop_reason is None else state.stop_reason.value,
    }


def _capability_schema_payload(schema: StructuredSchema) -> dict[str, Any]:
    return {
        "kind": schema.kind.value,
        "properties": {
            key: _capability_schema_payload(value)
            for key, value in schema.properties.items()
        },
        "required": list(schema.required),
        "items": None if schema.items is None else _capability_schema_payload(schema.items),
        "extra_properties": schema.extra_properties,
    }


def _context_payload(context: ReasoningContext) -> str:
    payload = {
        "goal": _state_payload(context.goal),
        "conversation": [
            {
                "conversation_id": item.conversation_id,
                "turn_id": item.turn_id,
                "origin": item.origin.value,
                "content": item.content,
                "occurred_at": item.occurred_at.isoformat(),
                "person_id": item.person_id,
            }
            for item in context.turns
        ],
        "available_memory_sources": [
            *(
                {"reference": f"turn:{item.turn_id}", "person_id": item.person_id}
                for item in context.turns
            ),
            *(
                {"reference": f"evidence:{item.evidence_id}", "person_id": None}
                for item in context.goal.evidence
            ),
            *(
                {"reference": f"decision:{item.record_id}", "person_id": None}
                for item in context.goal.decisions
            ),
            *(
                {"reference": f"correction:{item.record_id}", "person_id": None}
                for item in context.goal.corrections
            ),
            *(
                {"reference": f"progress:{item.record_id}", "person_id": None}
                for item in context.goal.progress
            ),
            *(
                {"reference": f"attempt:{item.call.call_id}", "person_id": None}
                for item in context.goal.attempts
                if item.call is not None
            ),
        ],
        "retrieved_memories": [
            {
                "memory_id": item.memory_id,
                "kind": item.kind.value,
                "person_id": item.person_id,
                "supersedes_memory_id": item.supersedes_memory_id,
                "content": item.current.content,
                "source_references": list(item.current.source_references),
                "formed_at": item.revisions[0].recorded_at.isoformat(),
                "revised_at": item.current.recorded_at.isoformat(),
                "revision_reason": item.current.reason,
                "meaning": item.current.meaning,
            }
            for item in context.memories
        ],
        "capabilities": [
            {
                "id": item.capability_id,
                "purpose": item.purpose,
                "side_effect": item.side_effect.value,
                "possible_failure_codes": list(item.possible_failure_codes),
                "input_schema": _capability_schema_payload(item.input_schema),
                "output_schema": _capability_schema_payload(item.output_schema),
            }
            for item in context.capabilities
        ],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _array(item_properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": dict(item_properties),
            "required": list(item_properties),
            "additionalProperties": False,
        },
    }


def decision_schema() -> dict[str, Any]:
    string = {"type": "string"}
    nullable_string = {"type": ["string", "null"]}
    progress = {"id": string, "summary": string, "evidence_refs": {"type": "array", "items": string}}
    properties: dict[str, Any] = {
        "action": {"type": "string", "enum": ["respond", "call_capability", "retrieve_memories"]},
        "response": nullable_string,
        "call_id": nullable_string,
        "capability_id": nullable_string,
        "arguments_json": nullable_string,
        "approval_id": nullable_string,
        "objective_summary": string,
        "success_criteria": _array({"id": string, "description": string}),
        "context_json": string,
        "referents": _array({"id": string, "attributes_json": string}),
        "new_decisions": _array(progress),
        "new_corrections": _array(progress),
        "new_progress": _array(progress),
        "blockers": _array({"id": string, "summary": string}),
        "outstanding_work": _array({"id": string, "summary": string}),
        "new_evidence": _array(
            {
                "id": string,
                "kind": string,
                "attributes_json": string,
                "supports": {"type": "array", "items": string},
            }
        ),
        "memory_proposals": _array(
            {
                "id": string,
                "kind": {"type": "string", "enum": [item.value for item in MemoryKind]},
                "content": string,
                "source_references": {"type": "array", "items": string},
                "formed_at": string,
                "person_id": nullable_string,
                "meaning": nullable_string,
                "supersedes_memory_id": nullable_string,
            }
        ),
        "memory_query_id": nullable_string,
        "memory_kinds": {"type": "array", "items": {"type": "string", "enum": [item.value for item in MemoryKind]}},
        "memory_ids": {"type": "array", "items": string},
        "memory_person_id": nullable_string,
        "memory_formed_after": nullable_string,
        "memory_formed_before": nullable_string,
        "memory_source_references": {"type": "array", "items": string},
        "memory_source_match": {"type": "string", "enum": [item.value for item in MemorySourceMatch]},
        "memory_include_superseded": {"type": "boolean"},
        "status": {"type": "string", "enum": [item.value for item in GoalStatus]},
        "stop_reason": {
            "type": ["string", "null"],
            "enum": [None, *[item.value for item in GoalStopReason]],
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


class ModelReasoner:
    """The sole model-backed implementation of the Core reasoning port."""

    def __init__(self, model: ReasoningModel, laws: str, identity: str) -> None:
        if not laws.strip() or not identity.strip():
            raise ValueError("approved Laws and identity context are required")
        self._model = model
        self._constitutional_context = f"{laws.strip()}\n\n{identity.strip()}"

    def decide(self, context: ReasoningContext) -> AgentDecision:
        completion = self._model.complete(
            ModelRequest(
                (
                    ModelMessage(ModelRole.SYSTEM, self._constitutional_context),
                    ModelMessage(ModelRole.SYSTEM, PROTOCOL_INSTRUCTIONS),
                    ModelMessage(ModelRole.USER, _context_payload(context)),
                ),
                "alx_core_decision",
                decision_schema(),
            )
        )
        output = completion.output
        objective = Objective(context.goal.objective.source_reference, output["objective_summary"])
        new_decisions = _without_reused_ids(
            context.goal.decisions,
            _records(output["new_decisions"], ProgressRecord, "new_decisions"),
            "record_id",
            "new_decisions",
        )
        new_corrections = _without_reused_ids(
            context.goal.corrections,
            _records(output["new_corrections"], ProgressRecord, "new_corrections"),
            "record_id",
            "new_corrections",
        )
        new_progress = _without_reused_ids(
            context.goal.progress,
            _records(output["new_progress"], ProgressRecord, "new_progress"),
            "record_id",
            "new_progress",
        )
        new_evidence = _without_reused_ids(
            context.goal.evidence,
            _records(output["new_evidence"], Evidence, "new_evidence"),
            "evidence_id",
            "new_evidence",
        )
        state = replace(
            context.goal,
            objective=objective,
            success_criteria=_records(output["success_criteria"], SuccessCriterion, "success_criteria"),
            context=_object_json(output["context_json"], "context_json"),
            referents=_records(output["referents"], Referent, "referents"),
            decisions=(*context.goal.decisions, *new_decisions),
            corrections=(*context.goal.corrections, *new_corrections),
            progress=(*context.goal.progress, *new_progress),
            blockers=_records(output["blockers"], WorkItem, "blockers"),
            outstanding_work=_records(output["outstanding_work"], WorkItem, "outstanding_work"),
            evidence=(*context.goal.evidence, *new_evidence),
            status=GoalStatus(output["status"]),
            stop_reason=None if output["stop_reason"] is None else GoalStopReason(output["stop_reason"]),
        )
        memory_proposals = tuple(
            MemoryProposal(
                item["id"],
                MemoryKind(item["kind"]),
                item["content"],
                _strings(item["source_references"], "memory source_references"),
                datetime.fromisoformat(item["formed_at"]),
                item["person_id"],
                item["meaning"],
                item["supersedes_memory_id"],
            )
            for item in output["memory_proposals"]
        )
        query_fields_present = any(
            (
                output["memory_query_id"] is not None,
                bool(output["memory_kinds"]),
                bool(output["memory_ids"]),
                output["memory_person_id"] is not None,
                output["memory_formed_after"] is not None,
                output["memory_formed_before"] is not None,
                bool(output["memory_source_references"]),
                output["memory_source_match"] != MemorySourceMatch.ANY.value,
                output["memory_include_superseded"],
            )
        )
        if output["action"] == "retrieve_memories":
            if output["response"] is not None or any(
                output[name] is not None
                for name in ("call_id", "capability_id", "arguments_json", "approval_id")
            ):
                raise ValueError("a memory query cannot include a response or capability call")
            if output["memory_query_id"] is None:
                raise ValueError("a memory query requires an identifier")
            query = MemoryQuery(
                output["memory_query_id"],
                tuple(MemoryKind(item) for item in output["memory_kinds"]),
                _strings(output["memory_ids"], "memory_ids"),
                output["memory_person_id"],
                None if output["memory_formed_after"] is None else datetime.fromisoformat(output["memory_formed_after"]),
                None if output["memory_formed_before"] is None else datetime.fromisoformat(output["memory_formed_before"]),
                _strings(output["memory_source_references"], "memory_source_references"),
                MemorySourceMatch(output["memory_source_match"]),
                output["memory_include_superseded"],
            )
            return AgentDecision(state, memory_proposals=memory_proposals, memory_query=query)
        if output["action"] == "respond":
            if query_fields_present or any(output[name] is not None for name in ("call_id", "capability_id", "arguments_json", "approval_id")):
                raise ValueError("a response cannot include another action")
            return AgentDecision(state, response=output["response"], memory_proposals=memory_proposals)
        if output["action"] != "call_capability" or output["response"] is not None or query_fields_present:
            raise ValueError("unknown or contradictory decision action")
        if any(output[name] is None for name in ("call_id", "capability_id", "arguments_json")):
            raise ValueError("a capability decision requires a complete call")
        call = CapabilityCall(
            output["call_id"],
            output["capability_id"],
            _object_json(output["arguments_json"], "arguments_json"),
            output["approval_id"],
        )
        return AgentDecision(state, call=call, memory_proposals=memory_proposals)
