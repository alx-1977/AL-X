"""What completion requires must be visible before it is enforced.

The live turn of 2026-09-11 10:06 UTC. Friedl asked for two emails to be
deleted. Both move_mail_message_to_trash calls executed and succeeded, both
observations became done, and the work was genuinely finished. Then:

    Goal proposal rejected: completion_lacks_sourced_evidence
    Core turn finished: state=error reason=goal_proposal_invalid response=False
    Voice session failed: goal_proposal_invalid

The goal had two success criteria, two succeeded attempts, and evidence: [].
Completion requires that every criterion be supported by an evidence item
citing a succeeded attempt, and the Core had created none. It had recorded a
progress record instead -- the right instinct in the wrong structure.

The rule was never the problem. It was invisible: PROTOCOL_INSTRUCTIONS never
mentioned request_completion, "completion requires" or "every criterion", and
the operation enum carried request_completion with no description. The Core
could only learn the contract by being refused, after spending a step, and the
refusal was terminal because the response depended on the commit.

Both attempts were offered as citable sources at the time, and the turn had
used three of twenty-five steps. Everything needed was present except the
knowledge that it was needed.

These tests hold the contract visible. They deliberately do not test that the
model complies -- that is nondeterministic -- and they do not touch the runtime
validation, which was correct throughout.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.core.model_reasoner import (  # noqa: E402
    PROTOCOL_INSTRUCTIONS, decision_schema,
)


class TheProtocolStatesTheCompletionRule(unittest.TestCase):
    """The precondition, in the stable cached prefix."""

    def test_the_protocol_names_the_completion_requirement(self) -> None:
        for stated in (
            "Completion is granted on evidence, not on assertion",
            "request_completion",
            "every success criterion of the goal is named in the supports field",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_protocol_requires_the_evidence_to_cite_the_attempt(self) -> None:
        """Not merely that evidence exists, but that it cites what happened."""
        for stated in (
            "cites the attempt that actually did the\nwork",
            "attempt:<call_id> from available_memory_sources",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_protocol_says_an_unsupported_criterion_refuses_completion(self) -> None:
        """The live outcome, stated before it happens rather than after."""
        self.assertIn(
            "A criterion nobody's evidence\nsupports leaves the goal unfinished",
            PROTOCOL_INSTRUCTIONS,
        )

    def test_the_protocol_distinguishes_evidence_from_progress_records(self) -> None:
        """The exact confusion the live goal showed: progress, not evidence."""
        self.assertIn(
            "Progress, decision and correction records do not carry this",
            PROTOCOL_INSTRUCTIONS,
        )

    def test_the_protocol_says_evidence_may_accompany_the_request(self) -> None:
        """No extra step is needed, which is what made this recoverable."""
        for stated in (
            "same goal mutation as the request_completion",
            "record the evidence and request completion\ntogether",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_rule_reaches_the_model_in_the_cached_prefix(self) -> None:
        """It belongs with the protocol, not in per-turn context."""
        from alx.contracts import (
            CapabilityDefinition, ConversationOrigin, ConversationTurn,
            GoalState, GoalSummary, ModelCompletion, Objective,
            ReasoningContext, SideEffect, StructuredSchema, SuccessCriterion,
            ValueKind,
        )
        from alx.core import ModelReasoner
        from datetime import UTC, datetime

        now = datetime(2026, 9, 11, tzinfo=UTC)
        schema = StructuredSchema(ValueKind.OBJECT)
        state = GoalState(
            "goal-1", Objective("turn:turn-1", "work"),
            (SuccessCriterion("criterion-1", "done"),),
        )

        class FakeModel:
            def __init__(self) -> None:
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                return ModelCompletion("fake", "fake-model", {
                    "goal_id": None, "goal_update": None, "memory_proposals": [],
                    "action": {
                        "type": "respond", "response": "ok",
                        "response_requires_goal_commit": False,
                    },
                })

        model = FakeModel()
        ModelReasoner(model, "laws", "identity").decide(ReasoningContext(
            state,
            (ConversationTurn(
                "conversation-1", "turn-1", ConversationOrigin.TYPED,
                "go", now, "friedl",
            ),),
            (CapabilityDefinition(
                "inspect", "Inspect", schema, schema, SideEffect.NONE,
            ),),
            unfinished_goals=(GoalSummary.of(state),),
        ))
        protocol = model.requests[0].messages[1].content
        self.assertIn("Completion is granted on evidence", protocol)


class TheSchemaStatesTheSameRule(unittest.TestCase):
    """The field the Core actually fills carries it too."""

    def _operation(self) -> dict:
        update = decision_schema()["properties"]["goal_update"]["anyOf"][1]
        return update["properties"]["operation"]

    def test_request_completion_is_offered_as_an_operation(self) -> None:
        self.assertIn("request_completion", self._operation()["enum"])

    def test_the_operation_field_describes_the_completion_precondition(self) -> None:
        """It previously carried request_completion with no description."""
        description = self._operation().get("description", "")
        self.assertIn("request_completion is accepted only when", description)
        self.assertIn("every success criterion", description)
        self.assertIn("succeeded attempt", description)
        self.assertIn("new_evidence in this same", description)


class TheStatedRuleMatchesTheEnforcedRule(unittest.TestCase):
    """Prose and enforcement must describe one rule, not two.

    The runtime check is unchanged and correct; this guards the direction that
    actually caused the incident, where enforcement moved ahead of what the
    Core had been told.
    """

    def test_the_runtime_requires_exactly_what_the_protocol_states(self) -> None:
        import inspect
        from alx.core.loop import CoreAgent

        source = inspect.getsource(CoreAgent._derive_goal_status)
        # Every criterion must be covered by evidence supports.
        self.assertIn("required = {item.criterion_id for item in state.success_criteria}", source)
        self.assertIn("if not required.issubset(supported):", source)
        # And that evidence must cite a succeeded attempt.
        self.assertIn("CapabilityResultState.SUCCEEDED", source)
        self.assertIn('reference.startswith("attempt:")', source)
        self.assertIn("completion_lacks_sourced_evidence", source)

    def test_the_refusal_reason_the_protocol_anticipates_still_exists(self) -> None:
        """If this reason is ever renamed, the guidance must follow it."""
        import inspect
        from alx.core.loop import CoreAgent

        self.assertIn(
            "completion_lacks_sourced_evidence",
            inspect.getsource(CoreAgent._derive_goal_status),
        )


if __name__ == "__main__":
    unittest.main()
