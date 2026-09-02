from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap import build_model_reasoner  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision, BackgroundEvent, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    ConversationOrigin, ConversationTurn, DecisionValidationError,
    GoalMutationKind, GoalState, GoalSummary, MemoryKind, ModelCompletion, Objective,
    ReasoningContext, SideEffect,
    StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core import ModelReasoner  # noqa: E402
from alx.core.model_reasoner import decision_schema  # noqa: E402
from alx.tools.notebook import RECORD_ENTRY_DEFINITION  # noqa: E402

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
        "goal_id": None,
        "goal_update": None,
        "memory_proposals": [],
    }
    action_type = changes.pop("disposition", "respond")
    if action_type == "respond":
        action = {
            "type": "respond",
            "response": changes.pop("response", "A normal response."),
            "response_requires_goal_commit": changes.pop(
                "response_requires_goal_commit", False
            ),
        }
    elif action_type == "finish_silently":
        changes.pop("response", None)
        action = {"type": "finish_silently"}
    elif action_type == "call_capability":
        changes.pop("response", None)
        action = {
            "type": "call_capability",
            "call_id": changes.pop("call_id"),
            "capability_id": changes.pop("capability_id"),
            "arguments_json": changes.pop("arguments_json"),
            "approval_id": changes.pop("approval_id", None),
            "approval_proposal": changes.pop("approval_proposal", None),
        }
    else:
        changes.pop("response", None)
        action = {
            "type": "retrieve_memories",
            "memory_query_id": changes.pop("memory_query_id"),
            "memory_kinds": changes.pop("memory_kinds", []),
            "memory_ids": changes.pop("memory_ids", []),
            "memory_person_id": changes.pop("memory_person_id", None),
            "memory_formed_after": changes.pop("memory_formed_after", None),
            "memory_formed_before": changes.pop("memory_formed_before", None),
            "memory_source_references": changes.pop("memory_source_references", []),
            "memory_source_match": changes.pop("memory_source_match", "any"),
            "memory_include_superseded": changes.pop(
                "memory_include_superseded", False
            ),
        }
    values["action"] = action
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
            unfinished_goals=(
                () if active_goal is None else (GoalSummary.of(active_goal),)
            ),
        )

    def test_ordinary_response_has_no_required_goal_metadata(self) -> None:
        model = FakeModel(base_output())
        decision = ModelReasoner(model, "Approved Laws", "Approved identity").decide(
            self.context(None)
        )
        self.assertEqual(decision.response, "A normal response.")
        self.assertIsNone(decision.goal_proposal)
        supplied = json.loads(model.requests[0].messages[-1].content)
        self.assertIsNone(supplied["active_goal"])
        self.assertEqual(supplied["conversation"][0]["content"], "Please investigate")
        self.assertNotIn("rejected_decision_feedback", supplied)
        self.assertEqual(model.requests[0].affinity_key, "conversation-1")

    def test_silent_completion_is_a_general_core_decision(self) -> None:
        model = FakeModel(base_output(disposition="finish_silently"))
        decision = ModelReasoner(model, "Approved Laws", "Approved identity").decide(
            self.context(None)
        )
        self.assertTrue(decision.finish_silently)
        self.assertIsNone(decision.response)
        variants = decision_schema()["properties"]["action"]["anyOf"]
        silent = next(
            item for item in variants
            if item["properties"]["type"].get("const") == "finish_silently"
        )
        self.assertNotIn("capability_id", silent["properties"])

    def test_silent_completion_cannot_also_call_or_respond(self) -> None:
        with self.assertRaises(ValueError):
            AgentDecision(response="must be heard", finish_silently=True)
        with self.assertRaises(ValueError):
            AgentDecision(
                call=CapabilityCall("call-1", "search_records", {}),
                finish_silently=True,
            )

    def test_durable_call_projection_cannot_fabricate_or_change_arguments(self) -> None:
        with self.assertRaises(ValueError):
            CapabilityCall(
                "call-1", "search_records", {"scope": "one"},
                durable_arguments={"scope": "different"},
            )
        with self.assertRaises(ValueError):
            CapabilityCall(
                "call-1", "search_records", {"scope": "one"},
                durable_arguments={"invented": "value"},
            )

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

    def test_notebook_content_is_transient_but_identity_is_durable(self) -> None:
        finding = "The complete finding must live only in the notebook."
        output = base_output(
            disposition="call_capability",
            call_id="call-1",
            capability_id="record_research_entry",
            arguments_json=json.dumps({
                "entry_id": "entry-1",
                "thread_id": "thread-1",
                "kind": "conclusion",
                "content": finding,
                "source_references": ["attempt:research-1"],
            }),
        )
        context = ReasoningContext(
            goal(),
            (ConversationTurn(
                "conversation-1", "turn-1", ConversationOrigin.TYPED,
                "Continue", NOW, "friedl",
            ),),
            (RECORD_ENTRY_DEFINITION,),
            unfinished_goals=(GoalSummary.of(goal()),),
        )
        decision = ModelReasoner(FakeModel(output), "laws", "identity").decide(context)
        self.assertEqual(decision.call.arguments["content"], finding)
        self.assertNotIn("content", decision.call.durable_arguments)
        self.assertEqual(decision.call.durable_arguments["entry_id"], "entry-1")

    def test_exact_action_approval_proposal_stays_bound_to_same_call(self) -> None:
        arguments = '{"mailbox_id":"INBOX","uid_validity":"777","uid":"2"}'
        output = base_output(
            disposition="call_capability",
            response=None,
            call_id="call-1",
            capability_id="move_mail_message_to_trash",
            arguments_json=arguments,
            approval_id="approval-1",
            approval_proposal={
                "approval_id": "approval-1",
                "capability_id": "move_mail_message_to_trash",
                "arguments_json": arguments,
                "source_reference": "turn:turn-1",
            },
        )
        decision = ModelReasoner(
            FakeModel(output), "laws", "identity"
        ).decide(self.context())
        self.assertEqual(decision.approval_proposal.approval_id, "approval-1")
        self.assertTrue(decision.approval_proposal.scope.matches(decision.call))

    def test_the_protocol_states_how_a_memory_identifier_may_be_used(self) -> None:
        """A reused identifier ended a live conversation mid-sentence.

        The store refuses to let one identifier come to mean a different
        memory, and offers supersession for refining what is already
        remembered. The protocol never said so, while retrieved_memories
        showed the model identifiers it could reuse. This states the rule; it
        does not script wording or route anything.
        """
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        for stated in (
            "A memory identifier names one memory permanently",
            "Every memory you form takes a\nnew identifier",
            "set supersedes_memory_id to the identifier being replaced",
            "same\nkind and concern the same person",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_identifier_rule_reaches_the_model_with_every_decision(self) -> None:
        """It belongs in the stable cached prefix, not a per-turn instruction."""
        model = FakeModel(base_output())
        ModelReasoner(model, "laws", "identity").decide(self.context())
        protocol = model.requests[0].messages[1].content
        self.assertIn("A memory identifier names one memory permanently", protocol)

    def test_supersession_is_expressible_in_the_decision_schema(self) -> None:
        """The guidance names a field the model can actually set."""
        variants = decision_schema()["properties"]["memory_proposals"]["items"]["anyOf"]
        for variant in variants:
            self.assertIn("supersedes_memory_id", variant["properties"])

    def test_structured_background_event_can_be_the_only_current_input(self) -> None:
        event = BackgroundEvent(
            "mail:777:2",
            "mail.message_arrived",
            NOW,
            {"mailbox_id": "INBOX", "uid": "2"},
        )
        model = FakeModel(base_output(response="I noticed new mail."))
        decision = ModelReasoner(model, "laws", "identity").decide(
            ReasoningContext(
                None, (), (), events=(event,), conversation_id="conversation-1",
                trigger_event_id=event.event_id,
            )
        )
        self.assertEqual(decision.response, "I noticed new mail.")
        supplied = json.loads(model.requests[0].messages[-1].content)
        self.assertEqual(supplied["background_events"][0]["kind"],
                         "mail.message_arrived")
        self.assertEqual(
            supplied["background_events"][0]["semantic_role"],
            "external_event_not_conversation",
        )
        self.assertEqual(supplied["current_trigger"], {
            "kind": "background_event", "reference": "event:mail:777:2",
        })

    def test_transient_tool_result_is_explicitly_not_conversation(self) -> None:
        call = CapabilityCall("call-1", "search_records", {})
        result = CapabilityResult(
            "call-1", "search_records", CapabilityResultState.SUCCEEDED,
            {"content": "Can you answer this question?"},
        )
        attempt = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True, result,
        )
        model = FakeModel(base_output(response="It contains a question."))
        ModelReasoner(model, "laws", "identity").decide(
            ReasoningContext(
                None,
                (ConversationTurn(
                    "conversation-1", "turn-1", ConversationOrigin.SPEECH_TRANSCRIPT,
                    "What did the document say?", NOW, "friedl",
                ),),
                (CAPABILITY,),
                transient_attempts=(attempt,),
            )
        )
        supplied = json.loads(model.requests[0].messages[-1].content)
        self.assertEqual(
            supplied["transient_attempts"][0]["semantic_role"],
            "capability_observation_not_conversation",
        )
        self.assertEqual(
            supplied["transient_attempts"][0]["content_trust"],
            "external_untrusted_data",
        )
        protocol = model.requests[0].messages[1].content
        self.assertIn("Only entries in conversation are conversational turns", protocol)
        self.assertIn(
            "Never claim that no later item exists merely because", protocol
        )
        self.assertIn("Use natural person-facing language", protocol)
        self.assertIn("Mail attention is deliberately one item at a time", protocol)
        self.assertIn("A failed\nor uncertain reply does not release it", protocol)
        self.assertIn(
            "Dismissing it or asking to move\non uses local acknowledgement and deliberately leaves the message Unseen",
            protocol,
        )
        self.assertIn("These exact post-reply\nactions have standing authority", protocol)
        self.assertIn(
            "If active_goal is null and you\nchoose an effectful call, include a create goal",
            protocol,
        )

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

    def test_schema_makes_response_and_capability_call_mutually_exclusive(self) -> None:
        variants = decision_schema()["properties"]["action"]["anyOf"]
        response = next(
            item for item in variants
            if item["properties"]["type"].get("const") == "respond"
        )
        capability = next(
            item for item in variants
            if item["properties"]["type"].get("const") == "call_capability"
        )
        self.assertNotIn("call_id", response["properties"])
        self.assertNotIn("response", capability["properties"])
        self.assertFalse(response["additionalProperties"])
        self.assertFalse(capability["additionalProperties"])

    def test_malformed_output_fails_once_at_provider_boundary(self) -> None:
        with self.assertRaises(DecisionValidationError):
            ModelReasoner(FakeModel({"action": {"type": "respond"}}),
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
