from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap import build_model_reasoner  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityDefinition,
    ConversationOrigin,
    ConversationTurn,
    GoalState,
    GoalStatus,
    MemoryKind,
    ModelCompletion,
    Objective,
    ReasoningContext,
    SideEffect,
    StructuredSchema,
    SuccessCriterion,
    ValueKind,
)
from alx.core import CoreAgent, CoreState, ModelReasoner  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.memories import MemoryNotFound, SQLiteMemoryStore  # noqa: E402


NOW = datetime(2026, 8, 27, tzinfo=UTC)
SCHEMA = StructuredSchema(
    ValueKind.OBJECT,
    properties={"record_type": StructuredSchema(ValueKind.STRING)},
    required=("record_type",),
    extra_properties=False,
)
CAPABILITY = CapabilityDefinition(
    "search_records",
    "find records using structured criteria",
    SCHEMA,
    SCHEMA,
    SideEffect.NONE,
)


def goal() -> GoalState:
    return GoalState(
        "goal-1",
        Objective("turn-1", "original objective"),
        (SuccessCriterion("criterion-1", "useful answer"),),
        context={"nested": {"value": 1}},
    )


def base_output(**changes):
    values = {
        "action": "respond",
        "response": "I disagree with that assumption, and here is why.",
        "call_id": None,
        "capability_id": None,
        "arguments_json": None,
        "approval_id": None,
        "objective_summary": "understand and answer the request",
        "success_criteria": [{"id": "criterion-1", "description": "useful answer"}],
        "context_json": json.dumps({"nested": {"value": 2}}),
        "referents": [],
        "new_decisions": [
            {"id": "decision-1", "summary": "challenge the assumption", "evidence_refs": []}
        ],
        "new_corrections": [],
        "new_progress": [],
        "blockers": [],
        "outstanding_work": [{"id": "continue-1", "summary": "await the next turn"}],
        "new_evidence": [],
        "memory_proposals": [],
        "status": "awaiting_input",
        "stop_reason": "required_input",
    }
    values.update(changes)
    return values


class FakeModel:
    def __init__(self, output):
        self.output = output
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelCompletion("fake", "fake-model", self.output)


class ModelReasonerTests(unittest.TestCase):
    def context(self):
        return ReasoningContext(
            goal(),
            (
                ConversationTurn(
                    "conversation-1",
                    "turn-1",
                    ConversationOrigin.SPEECH_TRANSCRIPT,
                    "Please challenge this assumption",
                    NOW,
                    "friedl",
                ),
            ),
            (CAPABILITY,),
        )

    def test_approved_context_and_durable_state_reach_one_model_path(self) -> None:
        model = FakeModel(base_output())
        reasoner = ModelReasoner(model, "Approved Laws", "Approved identity and origins")

        decision = reasoner.decide(self.context())

        self.assertEqual(decision.response, "I disagree with that assumption, and here is why.")
        self.assertEqual(decision.goal.status, GoalStatus.AWAITING_INPUT)
        self.assertEqual(decision.goal.context["nested"]["value"], 2)
        self.assertEqual(decision.goal.decisions[-1].summary, "challenge the assumption")
        request = model.requests[0]
        self.assertIn("Approved Laws", request.messages[0].content)
        self.assertIn("Approved identity and origins", request.messages[0].content)
        supplied = json.loads(request.messages[2].content)
        self.assertEqual(supplied["conversation"][0]["content"], "Please challenge this assumption")
        self.assertEqual(supplied["conversation"][0]["person_id"], "friedl")
        self.assertEqual(supplied["available_memory_sources"][0]["reference"], "turn:turn-1")
        self.assertEqual(supplied["capabilities"][0]["id"], "search_records")
        self.assertEqual(
            supplied["capabilities"][0]["input_schema"]["required"],
            ["record_type"],
        )
        self.assertEqual(supplied["goal"]["context"]["nested"]["value"], 1)

    def test_model_can_propose_one_language_blind_primitive_call(self) -> None:
        model = FakeModel(
            base_output(
                action="call_capability",
                response=None,
                call_id="call-1",
                capability_id="search_records",
                arguments_json=json.dumps({"record_type": "component"}),
                outstanding_work=[],
                status="active",
                stop_reason=None,
            )
        )
        decision = ModelReasoner(model, "laws", "identity").decide(self.context())
        self.assertIsNone(decision.response)
        self.assertEqual(decision.call.capability_id, "search_records")
        self.assertEqual(decision.call.arguments["record_type"], "component")

    def test_contradictory_or_malformed_model_output_fails_closed(self) -> None:
        contradictory = FakeModel(base_output(call_id="not-allowed-on-response"))
        with self.assertRaises(ValueError):
            ModelReasoner(contradictory, "laws", "identity").decide(self.context())
        malformed = FakeModel(base_output(context_json="not-json"))
        with self.assertRaises(json.JSONDecodeError):
            ModelReasoner(malformed, "laws", "identity").decide(self.context())
        duplicated = FakeModel(
            base_output(
                new_decisions=[
                    {"id": "same", "summary": "one", "evidence_refs": []},
                    {"id": "same", "summary": "two", "evidence_refs": []},
                ]
            )
        )
        with self.assertRaises(ValueError):
            ModelReasoner(duplicated, "laws", "identity").decide(self.context())
        repeated_memory = {
            "id": "same-memory",
            "kind": "factual",
            "content": "fact",
            "source_references": ["turn:turn-1"],
            "formed_at": NOW.isoformat(),
            "person_id": None,
            "meaning": None,
            "supersedes_memory_id": None,
        }
        with self.assertRaises(ValueError):
            ModelReasoner(
                FakeModel(base_output(memory_proposals=[repeated_memory, repeated_memory])),
                "laws",
                "identity",
            ).decide(self.context())

    def test_composition_root_loads_only_owner_approved_identity_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        model = FakeModel(base_output())
        reasoner = build_model_reasoner(model, root)
        reasoner.decide(self.context())
        constitutional = model.requests[0].messages[0].content
        self.assertIn("# Laws of AL/X", constitutional)
        self.assertIn("# AL/X Identity and Memory", constitutional)
        self.assertIn("Origin 04 — My history begins here", constitutional)

    def test_model_decision_returns_through_core_and_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            try:
                turn = self.context().turns[0]
                store.create(goal(), (turn,), NOW + timedelta(days=30))
                reasoner = ModelReasoner(FakeModel(base_output()), "laws", "identity")
                core = CoreAgent(
                    store,
                    reasoner,
                    lambda call, state: self.fail("a response must not dispatch"),
                    (CAPABILITY,),
                )

                outcome = core.run("goal-1", 1)

                self.assertEqual(outcome.state, CoreState.RESPONDED)
                recovered = store.load("goal-1")
                self.assertEqual(recovered.state.status, GoalStatus.AWAITING_INPUT)
                self.assertEqual(
                    recovered.state.decisions[-1].summary,
                    "challenge the assumption",
                )
            finally:
                store.close()

    def test_core_persists_semantically_selected_memory_with_real_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            goal_store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            memory_store = SQLiteMemoryStore(Path(directory) / "memories.sqlite3")
            try:
                turn = self.context().turns[0]
                goal_store.create(goal(), (turn,), NOW + timedelta(days=30))
                output = base_output(memory_proposals=[
                    {
                        "id": "memory-1",
                        "kind": "autobiographical",
                        "content": "I challenged an assumption instead of agreeing for approval.",
                        "source_references": ["turn:turn-1"],
                        "formed_at": NOW.isoformat(),
                        "person_id": None,
                        "meaning": "This reinforced my willingness to disagree respectfully.",
                        "supersedes_memory_id": None,
                    },
                    {
                        "id": "relationship-1",
                        "kind": "relationship",
                        "content": "Friedl welcomes evidence-based disagreement.",
                        "source_references": ["turn:turn-1"],
                        "formed_at": NOW.isoformat(),
                        "person_id": "friedl",
                        "meaning": None,
                        "supersedes_memory_id": None,
                    },
                ])
                core = CoreAgent(
                    goal_store,
                    ModelReasoner(FakeModel(output), "laws", "identity"),
                    lambda call, state: self.fail("a response must not dispatch"),
                    (CAPABILITY,),
                    memory_store,
                )

                outcome = core.run("goal-1", 1)

                self.assertEqual(outcome.state, CoreState.RESPONDED)
                remembered = memory_store.load("memory-1")
                self.assertEqual(remembered.kind, MemoryKind.AUTOBIOGRAPHICAL)
                self.assertEqual(remembered.current.source_references, ("turn:turn-1",))
                relationship = memory_store.load("relationship-1")
                self.assertEqual(relationship.person_id, "friedl")
            finally:
                goal_store.close()
                memory_store.close()

    def test_core_rejects_fabricated_source_and_cross_person_relationship(self) -> None:
        for proposal in (
            {
                "id": "fabricated",
                "kind": "factual",
                "content": "unsupported",
                "source_references": ["turn:missing"],
                "formed_at": NOW.isoformat(),
                "person_id": None,
                "meaning": None,
                "supersedes_memory_id": None,
            },
            {
                "id": "cross-person",
                "kind": "relationship",
                "content": "a preference",
                "source_references": ["turn:turn-1"],
                "formed_at": NOW.isoformat(),
                "person_id": "someone-else",
                "meaning": None,
                "supersedes_memory_id": None,
            },
        ):
            with self.subTest(memory_id=proposal["id"]), tempfile.TemporaryDirectory() as directory:
                goal_store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
                memory_store = SQLiteMemoryStore(Path(directory) / "memories.sqlite3")
                try:
                    turn = self.context().turns[0]
                    saved = goal_store.create(goal(), (turn,), NOW + timedelta(days=30))
                    core = CoreAgent(
                        goal_store,
                        ModelReasoner(FakeModel(base_output(memory_proposals=[proposal])), "laws", "identity"),
                        lambda call, state: self.fail("invalid memory must not dispatch"),
                        (CAPABILITY,),
                        memory_store,
                    )
                    outcome = core.run("goal-1", 1)
                    self.assertEqual(outcome.state, CoreState.ERROR)
                    self.assertEqual(outcome.reason, "memory_proposal_invalid")
                    self.assertEqual(goal_store.load("goal-1").revision, saved.revision)
                    with self.assertRaises(MemoryNotFound):
                        memory_store.load(proposal["id"])
                finally:
                    goal_store.close()
                    memory_store.close()


if __name__ == "__main__":
    unittest.main()
