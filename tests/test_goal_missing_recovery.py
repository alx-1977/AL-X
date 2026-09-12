"""A goal mutation refused for the wrong goal state must say so to the Core.

The live trace, 2026-09-11 09:03 UTC. Friedl said the two emails he had asked
to have deleted were still in his inbox. The Core decided to act, and the turn
went:

    step 1  call move_mail_message_to_trash, no goal
            -> "Dispatch blocked before approval: active_goal_required"
    step 2  call again, now offering an *update* goal mutation
            -> "Goal proposal rejected: goal_missing"
            -> "Dispatch blocked before approval: active_goal_required"
            -> session error, nothing dispatched

The mutation was the recoverable fault: an update needs a goal to update, and
this conversation had none, so the mutation that starts work is create. The
runtime knew that precisely -- it recorded goal_missing -- but the reason went
only to the log and the durable rejection record. What reached the Core was
active_goal_required, which names the dispatch prerequisite and says nothing
about the mutation kind. So the Core corrected the thing it was told about,
offered the same update again, and both steps were spent.

Goal-proposal rejections now reach refused_calls, the channel the Core already
reads for approval and memory rejections. Nothing else changes: the create path
that works today is untouched, and an accepted proposal never enters this code.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, GoalMutationKind, GoalProposal, GoalState, GoalStatus,
    Objective, SideEffect, StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.memories import SQLiteMemoryStore  # noqa: E402

NOW = datetime(2026, 9, 11, 9, 3, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)

# An effectful capability standing in for the live one. Nothing here is about
# mail: the rule is about which mutation may start work.
TRASH = CapabilityDefinition(
    "move_mail_message_to_trash", "Move one mail item to Trash",
    SCHEMA, SCHEMA, SideEffect.EFFECTFUL,
)


def conversation() -> ConversationSnapshot:
    return ConversationSnapshot(
        "conversation-1",
        (ConversationTurn(
            "conversation-1", "turn-1", ConversationOrigin.TYPED,
            "you did not delete them, they are still in my inbox", NOW,
            "friedl",
        ),),
        1,
        RETENTION,
    )


def call() -> CapabilityCall:
    return CapabilityCall(
        "call-trash-1", "move_mail_message_to_trash",
        {"mailbox_id": "INBOX", "uid_validity": "1376545928", "uid": "59134"},
    )


def create_proposal() -> GoalProposal:
    return GoalProposal(
        GoalMutationKind.CREATE,
        objective_summary="Delete the DigiKey and ClearScore emails",
        success_criteria=(SuccessCriterion("criterion-1", "both in Trash"),),
    )


class Queued:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")
        self.addCleanup(self.store.close)
        self.dispatched: list[CapabilityCall] = []

    def agent(self, reasoner) -> CoreAgent:
        def dispatch(item, state):
            self.dispatched.append(item)
            return CapabilityAttempt(
                item, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(
                    item.call_id, item.capability_id,
                    CapabilityResultState.SUCCEEDED, {},
                ),
            )

        return CoreAgent(
            self.store, reasoner, dispatch, (TRASH,),
            memory_store=SQLiteMemoryStore(
                Path(self.directory.name) / "memories.sqlite3"
            ),
            clock=lambda: NOW,
            identifier_factory=lambda: "goal-1",
        )


class TheLiveComplaintTrace(Harness):
    """The exact 09:03 sequence, and its recovery."""

    def test_an_update_with_no_goal_is_refused_as_goal_missing(self) -> None:
        """Steps 1-3 of the trace: the specific reason now reaches the Core."""
        reasoner = Queued(
            # The live step 2: effectful call carrying an update mutation,
            # with no goal in the conversation.
            AgentDecision(
                call=call(),
                goal_proposal=GoalProposal(GoalMutationKind.UPDATE),
            ),
            AgentDecision(response="I cannot do that right now."),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)

        refusals = reasoner.contexts[1].refused_calls
        reasons = {item["reason"] for item in refusals}
        # The mutation fault is named, not only the dispatch prerequisite.
        self.assertIn("goal_missing", reasons)
        entry = next(item for item in refusals if item["reason"] == "goal_missing")
        self.assertEqual(entry["mutation_kind"], "update")
        # And the dispatch refusal is still reported alongside it, because both
        # facts are true: the mutation was wrong and nothing was dispatched.
        self.assertIn("active_goal_required", reasons)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(outcome.state, CoreState.RESPONDED)

    def test_the_core_can_correct_to_create_and_the_call_proceeds(self) -> None:
        """Steps 4-5: correction inside the same turn, on the existing path."""
        reasoner = Queued(
            AgentDecision(
                call=call(),
                goal_proposal=GoalProposal(GoalMutationKind.UPDATE),
            ),
            # Corrected: the mutation that starts work, with the same call.
            AgentDecision(call=call(), goal_proposal=create_proposal()),
            AgentDecision(response="Both are in Trash now."),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 3)

        # The call ran through the ordinary create-goal-and-dispatch path.
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(
            self.dispatched[0].capability_id, "move_mail_message_to_trash"
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        stored = self.store.load("goal-1").state
        self.assertEqual(stored.status, GoalStatus.ACTIVE)
        self.assertEqual(len(stored.attempts), 1)
        self.assertIs(
            stored.attempts[0].disposition, CapabilityAttemptDisposition.EXECUTED
        )

    def test_the_live_failure_is_recoverable_within_the_step_budget(self) -> None:
        """The turn that died had two steps. Two steps are now enough.

        The runtime could always dispatch on step 2; what it could not do was
        tell the Core why step 1 failed. So the load-bearing assertion is that
        the correcting step is *given* goal_missing and the mutation kind --
        without that the second step is only a guess, which is what the live
        turn made and got wrong.
        """
        reasoner = Queued(
            AgentDecision(
                call=call(),
                goal_proposal=GoalProposal(GoalMutationKind.UPDATE),
            ),
            AgentDecision(call=call(), goal_proposal=create_proposal()),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)

        correcting_step = reasoner.contexts[1]
        entry = next(
            item for item in correcting_step.refused_calls
            if item["reason"] == "goal_missing"
        )
        self.assertEqual(entry["mutation_kind"], "update")
        self.assertEqual(len(self.dispatched), 1)
        self.assertNotEqual(outcome.state, CoreState.ERROR)

    def test_repeating_the_same_refused_mutation_ends_the_turn(self) -> None:
        """Told once and offered again unchanged: no unbounded retrying.

        The existing _already_refused rule, applied to this reason like every
        other. It adds no retries to the step loop.
        """
        reasoner = Queued(
            AgentDecision(
                call=call(),
                goal_proposal=GoalProposal(GoalMutationKind.UPDATE),
            ),
            AgentDecision(
                call=call(),
                goal_proposal=GoalProposal(GoalMutationKind.UPDATE),
            ),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 4)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        self.assertEqual(self.dispatched, [])


class OrdinaryGoalBehaviourIsUnchanged(Harness):
    """The working paths must not move."""

    def test_create_with_a_call_still_dispatches_in_one_step(self) -> None:
        """The path 49 live goals already take."""
        reasoner = Queued(
            AgentDecision(call=call(), goal_proposal=create_proposal()),
            AgentDecision(response="Done."),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        # Nothing was refused: an accepted proposal never enters that code.
        self.assertEqual(reasoner.contexts[1].refused_calls, ())

    def test_update_against_an_existing_goal_is_unaffected(self) -> None:
        self.store.create(
            GoalState(
                "goal-1", Objective("turn:turn-1", "Delete the two emails"),
                (SuccessCriterion("criterion-1", "both in Trash"),),
            ),
            "conversation-1", RETENTION,
        )
        reasoner = Queued(
            AgentDecision(
                goal_id="goal-1",
                call=call(),
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE,
                    objective_summary="Delete the two emails, revised",
                ),
            ),
            AgentDecision(goal_id="goal-1", response="Done."),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(reasoner.contexts[1].refused_calls, ())
        self.assertEqual(
            self.store.load("goal-1").state.objective.summary,
            "Delete the two emails, revised",
        )

    def test_an_ordinary_response_never_touches_this_path(self) -> None:
        reasoner = Queued(AgentDecision(response="The part arrives Thursday."))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "The part arrives Thursday.")
        self.assertEqual(self.dispatched, [])

    def test_a_response_depending_on_the_rejected_commit_still_errors(self) -> None:
        """Unchanged: that branch returns before the refusal is recorded."""
        reasoner = Queued(
            AgentDecision(
                response="Recorded.",
                response_requires_goal_commit=True,
                goal_proposal=GoalProposal(GoalMutationKind.UPDATE),
            ),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")


class TheProtocolExplainsTheReason(unittest.TestCase):
    def test_the_protocol_says_what_goal_missing_means(self) -> None:
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        for stated in (
            "An entry carrying mutation_kind refused the goal mutation",
            "goal_missing means you proposed a mutation of a goal that does not exist",
            "the mutation\nthat starts work is create",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
