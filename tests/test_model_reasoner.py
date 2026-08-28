from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap import build_model_reasoner  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityDefinition, ConversationOrigin, ConversationTurn,
    DecisionValidationError, GoalMutationKind, GoalState, MemoryKind,
    ModelCompletion, Objective, ReasoningContext, SideEffect,
    StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core import ModelReasoner  # noqa: E402
from alx.core.model_reasoner import decision_schema  # noqa: E402

NOW = datetime(2026, 8, 28, tzinfo=UTC)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
CAPABILITY = CapabilityDefinition(
    "search_records", "Search structured records", SCHEMA, SCHEMA,
    SideEffect.NONE,
)


def goal() -> GoalState:
    return GoalState(
        "goal-1", Objective("turn:turn-1", "investigate"),
        (SuccessCriterion("criterion-1", "verified"),),
    )


def base_output(**changes):
    values = {
        "disposition": "respond",
        "response": "A normal response.",
        "response_requires_goal_commit": False,
        "call_id": None,
        "capability_id": None,
        "arguments_json": None,
        "approval_id": None,
        "goal_update": None,
        "memory_proposals": [],
        "memory_query_id": None,
        "memory_kinds": [],
        "memory_ids": [],
        "memory_person_id": None,
        "memory_formed_after": None,
        "memory_formed_before": None,
        "memory_source_references": [],
        "memory_source_match": "any",
        "memory_include_superseded": False,
    }
    values.update(changes)
    return values


def goal_update(operation="update", **changes):
    values = {
        "operation": operation,
        "objective_summary": None,
        "success_criteria": None,
        "context_json": None,
        "referents": None,
        "new_decisions": [],
        "new_corrections": [],
        "new_progress": [],
        "blockers": None,
        "outstanding_work": None,
        "new_evidence": [],
    }
    values.update(changes)
    return values


class FakeModel:
    def __init__(self, output) -> None:
        self.output = output
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelCompletion("fake", "fake-model", self.output)


class ModelReasonerTests(unittest.TestCase):
    def context(self, active_goal=goal()):
        return ReasoningContext(
            active_goal,
            (ConversationTurn("conversation-1", "turn-1",
                              ConversationOrigin.SPEECH_TRANSCRIPT,
                              "Please investigate", NOW, "friedl"),),
            (CAPABILITY,),
        )

    def test_ordinary_response_has_no_required_goal_metadata(self) -> None:
        model = FakeModel(base_output())
        decision = ModelReasoner(model, "Approved Laws", "Approved identity").decide(
            self.context(None)
        )
        self.assertEqual(decision.response, "A normal response.")
        self.assertIsNone(decision.goal_proposal)
        supplied = json.loads(model.requests[0].messages[2].content)
        self.assertIsNone(supplied["active_goal"])
        self.assertEqual(supplied["conversation"][0]["content"], "Please investigate")
        self.assertNotIn("rejected_decision_feedback", supplied)
        self.assertEqual(model.requests[0].affinity_key, "conversation-1")

    def test_goal_output_is_a_proposal_not_replacement_state(self) -> None:
        update = goal_update(
            "request_completion",
            new_evidence=[{
                "id": "evidence-1", "kind": "observation",
                "attributes_json": "{}", "supports": ["criterion-1"],
                "source_references": ["turn:turn-1"],
            }],
        )
        decision = ModelReasoner(FakeModel(base_output(
            response="Verified.", response_requires_goal_commit=True,
            goal_update=update,
        )), "laws", "identity").decide(self.context())
        self.assertEqual(decision.goal_proposal.kind, GoalMutationKind.REQUEST_COMPLETION)
        self.assertEqual(
            decision.goal_proposal.new_evidence[0].source_references,
            ("turn:turn-1",),
        )
        self.assertTrue(decision.response_requires_goal_commit)
        self.assertFalse(hasattr(decision, "goal"))

    def test_language_blind_capability_call_remains_one_core_action(self) -> None:
        output = base_output(
            disposition="call_capability", response=None,
            call_id="call-1", capability_id="search_records",
            arguments_json=json.dumps({"record_type": "note"}),
        )
        decision = ModelReasoner(FakeModel(output), "laws", "identity").decide(
            self.context()
        )
        self.assertEqual(decision.call.capability_id, "search_records")
        self.assertEqual(decision.call.arguments["record_type"], "note")

    def test_memory_proposal_retains_semantic_choice_and_real_source(self) -> None:
        proposal = {
            "id": "memory-1", "kind": MemoryKind.AUTOBIOGRAPHICAL.value,
            "content": "I challenged an assumption.",
            "source_references": ["turn:turn-1"],
            "supersedes_memory_id": None, "person_id": None,
            "meaning": "I became more willing to challenge weak assumptions.",
        }
        decision = ModelReasoner(FakeModel(base_output(memory_proposals=[proposal])),
                                 "laws", "identity").decide(self.context())
        self.assertEqual(decision.memory_proposals[0].kind,
                         MemoryKind.AUTOBIOGRAPHICAL)
        self.assertEqual(decision.memory_proposals[0].formed_at, NOW)

    def test_schema_requires_sourced_evidence_and_has_no_model_completion_flag(self) -> None:
        schema = decision_schema()
        update = schema["properties"]["goal_update"]["anyOf"][1]
        evidence = update["properties"]["new_evidence"]["items"]
        self.assertIn("source_references", evidence["required"])
        encoded = json.dumps(schema)
        self.assertNotIn('"complete"', encoded)
        self.assertNotIn("respond_completed", encoded)

    def test_malformed_output_fails_once_at_provider_boundary(self) -> None:
        with self.assertRaises(DecisionValidationError):
            ModelReasoner(FakeModel({"disposition": "respond"}),
                          "laws", "identity").decide(self.context())

    def test_composition_root_loads_only_approved_identity_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        model = FakeModel(base_output())
        build_model_reasoner(model, root).decide(self.context(None))
        constitutional = model.requests[0].messages[0].content
        self.assertIn("Laws of AL/X", constitutional)
        self.assertIn("AL/X Identity and Memory", constitutional)


if __name__ == "__main__":
    unittest.main()
