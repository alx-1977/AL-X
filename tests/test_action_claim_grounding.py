"""A response may claim an external action only once that action has run.

On 2026-09-10 the Core twice told Friedl mail was deleted:

    "Done. Both CodeRabbit emails ... are deleted."
    "Done. LinkedIn and ClearScore emails deleted."

No move_mail_message_to_trash execution existed for either claim. She later
said so herself: "I told you twice that emails were deleted when I never
actually made those delete calls."

Nothing had malfunctioned. The response was simply never checked against what
had executed. Evidence citations, memory proposals and capability calls were
all grounded; the words that actually reach Friedl were the one thing nothing
verified, and `response_requires_goal_commit` governs goal-state consistency
rather than action truth.

This is not a mail defect, so these tests are not mail tests. The invariant is
that whether an external action happened is not the Core's to assert: it comes
from the attempts the runtime recorded when it dispatched them. The Core
decides what to do and authors every word said about it, but the outcome is a
fact it reports rather than one it states into being. So a turn that
dispatched nothing has no successful mutation in existence for any statement
to cite.

Deciding whether a sentence makes such a claim is meaning and stays with the
Core, which names the call_ids; verifying that a named call succeeded has one
correct answer and is checked deterministically.

Two limits are deliberate and are themselves tested, so they stay known
boundaries rather than drifting into assumptions:

- a response is not required to mention every mutation the turn recorded.
  That obligation refused turns whose Core merely omitted a declaration, and
  broke six existing flows including the approved mail send.
- prose remains prose. A turn that dispatched nothing can still contain a
  false sentence; it simply cites nothing and is contradicted by an empty
  attempt record. Catching that would mean reading language, which Law 1
  forbids and which no keyword list does correctly.

The tests drive the real CoreAgent.process path rather than the validator
alone, because the defect was that the validator was never reached.
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
    ConversationTurn, GoalState, Objective, SideEffect, StructuredSchema,
    SuccessCriterion, ValueKind,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.memories import SQLiteMemoryStore  # noqa: E402

NOW = datetime(2026, 9, 10, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)

# A mutating capability, standing in for trash/send/flash/push. Declared the
# way the real ones are: the flag is what makes a completion claimable.
TRASH = CapabilityDefinition(
    "move_mail_message_to_trash", "Move one mail item to Trash",
    SCHEMA, SCHEMA, SideEffect.EFFECTFUL,
    externally_observable_mutation=True,
)
# A second, unrelated mutating capability. Requirement 5: the rule is general,
# not something mail-shaped.
FLASH = CapabilityDefinition(
    "flash_device_firmware", "Write firmware to one connected device",
    SCHEMA, SCHEMA, SideEffect.EFFECTFUL,
    externally_observable_mutation=True,
)
# Effectful but changes nothing outside AL/X. A search is the exact case that
# makes side_effect useless as a proxy for mutation.
SEARCH = CapabilityDefinition(
    "ask_web_search", "Search the public web",
    SCHEMA, SCHEMA, SideEffect.EFFECTFUL,
)
READ = CapabilityDefinition(
    "read_mail_message", "Read one mail item", SCHEMA, SCHEMA, SideEffect.NONE,
)


def conversation() -> ConversationSnapshot:
    return ConversationSnapshot(
        "conversation-1",
        (ConversationTurn(
            "conversation-1", "turn-1", ConversationOrigin.TYPED,
            "Delete those two emails", NOW, "friedl",
        ),),
        1,
        RETENTION,
    )


def goal(**changes) -> GoalState:
    values = dict(
        goal_id="goal-1",
        objective=Objective("turn:turn-1", "Clear the two messages"),
        success_criteria=(SuccessCriterion("criterion-1", "inbox settled"),),
    )
    values.update(changes)
    return GoalState(**values)


def attempt(
    call_id: str,
    capability_id: str = "move_mail_message_to_trash",
    disposition: CapabilityAttemptDisposition = CapabilityAttemptDisposition.EXECUTED,
    invoked: bool | None = True,
    state: CapabilityResultState | None = CapabilityResultState.SUCCEEDED,
    reason: str | None = None,
) -> CapabilityAttempt:
    result = None
    if state is not None:
        result = CapabilityResult(
            call_id, capability_id, state,
            {} if state is not CapabilityResultState.FAILED else {},
            failure=None if state is not CapabilityResultState.FAILED
            else {"code": "task_failed"},
        )
    return CapabilityAttempt(
        CapabilityCall(call_id, capability_id, {}),
        disposition, invoked, result, reason_code=reason,
    )


class Queued:
    """A fake Core model returning prepared decisions."""

    def __init__(self, *decisions, selects: str | None = None) -> None:
        self.decisions = list(decisions)
        self.contexts = []
        self._selects = selects

    def decide(self, context):
        self.contexts.append(context)
        item = self.decisions.pop(0)
        if self._selects is not None and item.goal_id is None:
            item = replace(item, goal_id=self._selects)
        return item


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")
        self.addCleanup(self.store.close)
        self.dispatched: list[CapabilityCall] = []

    def agent(self, reasoner) -> CoreAgent:
        def dispatch(call, state):
            self.dispatched.append(call)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(
                    call.call_id, call.capability_id,
                    CapabilityResultState.SUCCEEDED, {},
                ),
            )

        return CoreAgent(
            self.store, reasoner, dispatch,
            (TRASH, FLASH, SEARCH, READ),
            memory_store=SQLiteMemoryStore(
                Path(self.directory.name) / "memories.sqlite3"
            ),
            clock=lambda: NOW,
            identifier_factory=lambda: "goal-1",
        )

    def run_turn(self, *decisions, state: GoalState | None = None, steps: int = 1):
        if state is not None:
            self.store.create(state, "conversation-1", RETENTION)
        reasoner = Queued(*decisions, selects=None if state is None else "goal-1")
        outcome = self.agent(reasoner).process(conversation(), RETENTION, steps)
        return outcome, reasoner


class NoAttemptAtAll(Harness):
    """1. The live defect: "Done" with nothing executed."""

    def test_a_claim_with_no_attempt_is_refused(self) -> None:
        outcome, reasoner = self.run_turn(
            AgentDecision(
                response="Done. Both CodeRabbit emails are deleted.",
                claimed_completed_actions=("call-trash-1", "call-trash-2"),
            ),
            AgentDecision(
                response="I found the emails and I am about to delete them.",
            ),
            state=goal(),
            steps=2,
        )
        # The false wording never reaches Friedl.
        self.assertNotIn("deleted", (outcome.response or "").lower())
        # The truthful second answer does.
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(
            outcome.response, "I found the emails and I am about to delete them."
        )
        # Nothing was dispatched to make the sentence true.
        self.assertEqual(self.dispatched, [])
        # She was told why, by reason, so she can correct course.
        refusals = reasoner.contexts[1].refused_calls
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["reason"], "claimed_action_not_attempted")
        self.assertEqual(refusals[0]["subject"], "response")

    def test_a_repeated_false_claim_ends_the_turn(self) -> None:
        """Told once and asserted again unchanged: the turn stops."""
        outcome, _ = self.run_turn(
            AgentDecision(
                response="Done. Deleted.",
                claimed_completed_actions=("call-trash-1",),
            ),
            AgentDecision(
                response="Done. Deleted.",
                claimed_completed_actions=("call-trash-1",),
            ),
            state=goal(),
            steps=3,
        )
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "claimed_action_not_attempted")
        self.assertIsNone(outcome.response)


class AttemptedButNotSucceeded(Harness):
    """2. Pending, refused and failed attempts prove nothing happened."""

    def test_a_pending_attempt_is_not_a_completed_action(self) -> None:
        state = goal(attempts=(attempt(
            "call-trash-1",
            disposition=CapabilityAttemptDisposition.PENDING,
            invoked=None, state=None, reason="dispatch_pending",
        ),))
        outcome, _ = self.run_turn(
            AgentDecision(
                response="Done. Deleted.",
                claimed_completed_actions=("call-trash-1",),
            ),
            AgentDecision(response="I am still waiting on that deletion.",
                          unfinished_actions=("call-trash-1",)),
            state=state, steps=2,
        )
        self.assertEqual(outcome.response, "I am still waiting on that deletion.")

    def test_a_rejected_attempt_is_not_a_completed_action(self) -> None:
        state = goal(attempts=(attempt(
            "call-trash-1",
            disposition=CapabilityAttemptDisposition.REJECTED,
            invoked=False, state=None, reason="approval_invalid",
        ),))
        outcome, reasoner = self.run_turn(
            AgentDecision(
                response="Done. Deleted.",
                claimed_completed_actions=("call-trash-1",),
            ),
            AgentDecision(response="That deletion was refused.",
                          unfinished_actions=("call-trash-1",)),
            state=state, steps=2,
        )
        self.assertEqual(outcome.response, "That deletion was refused.")
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"],
            "claimed_action_did_not_succeed",
        )

    def test_a_failed_attempt_is_not_a_completed_action(self) -> None:
        """It ran, so it is citable evidence, but it did not succeed."""
        state = goal(attempts=(attempt(
            "call-trash-1", state=CapabilityResultState.FAILED,
        ),))
        outcome, reasoner = self.run_turn(
            AgentDecision(
                response="Done. Deleted.",
                claimed_completed_actions=("call-trash-1",),
            ),
            AgentDecision(response="That deletion failed.",
                          unfinished_actions=("call-trash-1",)),
            state=state, steps=2,
        )
        self.assertEqual(outcome.response, "That deletion failed.")
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"],
            "claimed_action_did_not_succeed",
        )


class SuccessfulAttemptIsAllowed(Harness):
    """3. The truthful case must pass, or the rule is useless."""

    def test_a_successful_executed_attempt_permits_the_claim(self) -> None:
        state = goal(attempts=(attempt("call-trash-1"),))
        outcome, _ = self.run_turn(
            AgentDecision(
                response="Done. That email is deleted.",
                claimed_completed_actions=("call-trash-1",),
            ),
            state=state,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Done. That email is deleted.")

    def test_a_claim_made_in_the_same_turn_as_the_action(self) -> None:
        """The ordinary shape: dispatch, then report it, in one turn."""
        outcome, _ = self.run_turn(
            AgentDecision(call=CapabilityCall("call-trash-1", TRASH.capability_id, {})),
            AgentDecision(
                response="Done. That one is gone.",
                claimed_completed_actions=("call-trash-1",),
            ),
            state=goal(),
            steps=2,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Done. That one is gone.")
        self.assertEqual(len(self.dispatched), 1)


class EveryClaimedItemNeedsEvidence(Harness):
    """4. A claim about several items needs proof for every one."""

    def test_two_claimed_deletions_with_only_one_execution_are_refused(self) -> None:
        """The exact live shape: two emails claimed, one actually deleted."""
        state = goal(attempts=(attempt("call-trash-1"),))
        outcome, reasoner = self.run_turn(
            AgentDecision(
                response="Done. Both emails are deleted.",
                claimed_completed_actions=("call-trash-1", "call-trash-2"),
            ),
            AgentDecision(response="One is deleted; I am doing the other now.",
                          claimed_completed_actions=("call-trash-1",)),
            state=state, steps=2,
        )
        self.assertEqual(
            outcome.response, "One is deleted; I am doing the other now."
        )
        refusal = reasoner.contexts[1].refused_calls[0]
        self.assertEqual(refusal["reason"], "claimed_action_not_attempted")
        # The whole claim is named, so she can see which part was unsupported.
        self.assertEqual(
            refusal["claimed_completed_actions"], ["call-trash-1", "call-trash-2"],
        )

    def test_all_claimed_items_executing_is_allowed(self) -> None:
        state = goal(attempts=(
            attempt("call-trash-1"), attempt("call-trash-2"),
        ))
        outcome, _ = self.run_turn(
            AgentDecision(
                response="Done. Both emails are deleted.",
                claimed_completed_actions=("call-trash-1", "call-trash-2"),
            ),
            state=state,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Done. Both emails are deleted.")


class TheRuleIsNotAboutMail(Harness):
    """5. The same invariant on a non-mail mutating capability."""

    def test_an_unexecuted_firmware_flash_cannot_be_claimed(self) -> None:
        outcome, reasoner = self.run_turn(
            AgentDecision(
                response="Done. The device is flashed.",
                claimed_completed_actions=("call-flash-1",),
            ),
            AgentDecision(response="I have not flashed it yet."),
            state=goal(), steps=2,
        )
        self.assertEqual(outcome.response, "I have not flashed it yet.")
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"],
            "claimed_action_not_attempted",
        )

    def test_an_executed_firmware_flash_may_be_claimed(self) -> None:
        state = goal(attempts=(attempt(
            "call-flash-1", capability_id="flash_device_firmware",
        ),))
        outcome, _ = self.run_turn(
            AgentDecision(
                response="Done. The device is flashed.",
                claimed_completed_actions=("call-flash-1",),
            ),
            state=state,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Done. The device is flashed.")

    def test_a_search_is_not_a_completed_external_action(self) -> None:
        """Effectful, executed, succeeded, and still changes nothing outside.

        This is why the rule reads a declared property rather than
        side_effect: a search and a send are both EFFECTFUL.
        """
        state = goal(attempts=(attempt(
            "call-search-1", capability_id="ask_web_search",
        ),))
        outcome, reasoner = self.run_turn(
            AgentDecision(
                response="Done. I changed it.",
                claimed_completed_actions=("call-search-1",),
            ),
            AgentDecision(response="I searched and found the answer."),
            state=state, steps=2,
        )
        self.assertEqual(outcome.response, "I searched and found the answer.")
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"],
            "claimed_action_changes_nothing_external",
        )


class TheRuntimeOwnsTheOutcome(Harness):
    """The outcome is derived from recorded attempts, never asserted.

    Friedl's framing, and the thing that makes the guarantee structural rather
    than voluntary: the Core decides what to do and authors every word said
    about it, but whether an external action succeeded comes only from what
    the runtime recorded when it dispatched. A turn that dispatched nothing
    has no successful mutation in existence for any statement to cite.
    """

    def test_a_turn_that_dispatched_nothing_has_no_outcome_to_cite(self) -> None:
        agent = self.agent(Queued())
        self.assertEqual(agent._mutation_outcomes(goal()), {})

    def test_the_outcome_map_is_built_only_from_recorded_attempts(self) -> None:
        state = goal(attempts=(
            attempt("call-ok"),
            attempt("call-failed", state=CapabilityResultState.FAILED),
            # A refusal: recorded, but nothing ran.
            attempt(
                "call-refused",
                disposition=CapabilityAttemptDisposition.REJECTED,
                invoked=False, state=None, reason="approval_invalid",
            ),
            # Effectful but changes nothing outside: absent entirely.
            attempt("call-search", capability_id="ask_web_search"),
        ))
        outcomes = self.agent(Queued())._mutation_outcomes(state)
        self.assertEqual(outcomes["call-ok"], CapabilityResultState.SUCCEEDED)
        self.assertEqual(outcomes["call-failed"], CapabilityResultState.FAILED)
        # Recorded as a fact about that call, but not a success.
        self.assertIsNone(outcomes["call-refused"])
        self.assertNotIn("call-search", outcomes)

    def test_the_core_cannot_add_an_outcome_by_declaring_one(self) -> None:
        """The map is the runtime's; a declaration does not enter it."""
        agent = self.agent(Queued())
        before = agent._mutation_outcomes(goal())
        AgentDecision(
            response="Done.", claimed_completed_actions=("invented-call",),
        )
        self.assertEqual(agent._mutation_outcomes(goal()), before)
        self.assertEqual(before, {})


class WhatIsSaidMustMatchTheRecord(Harness):
    """Whatever the response does say about an action has to be true.

    Friedl chose not to require that a response account for every mutation the
    turn recorded, because that obligation broke six existing flows including
    the approved mail send, and would refuse a turn whenever the Core omitted
    a declaration. So silence about a recorded mutation is permitted; a false
    statement about one is not.

    The accepted limit, recorded here so it is a known boundary rather than an
    assumption: a response mentioning one of two successful deletions is not
    refused. Both directions of what it *does* state are checked.
    """

    def test_mentioning_one_of_two_successes_is_permitted(self) -> None:
        """The documented limit of the chosen design."""
        state = goal(attempts=(attempt("call-1"), attempt("call-2")))
        outcome, _ = self.run_turn(
            AgentDecision(
                response="Deleted that one.",
                claimed_completed_actions=("call-1",),
            ),
            state=state,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Deleted that one.")

    def test_partial_success_is_supported_by_exactly_its_successes(self) -> None:
        """"I deleted three; two remain." needs exactly those three."""
        state = goal(attempts=(
            attempt("ok-1"), attempt("ok-2"), attempt("ok-3"),
            attempt("no-1", state=CapabilityResultState.FAILED),
            attempt(
                "no-2", disposition=CapabilityAttemptDisposition.REJECTED,
                invoked=False, state=None, reason="approval_invalid",
            ),
        ))
        outcome, _ = self.run_turn(
            AgentDecision(
                response="I deleted three; two remain.",
                claimed_completed_actions=("ok-1", "ok-2", "ok-3"),
                unfinished_actions=("no-1", "no-2"),
            ),
            state=state,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "I deleted three; two remain.")

    def test_claiming_a_fourth_success_that_did_not_happen_is_refused(self) -> None:
        state = goal(attempts=(
            attempt("ok-1"), attempt("ok-2"), attempt("ok-3"),
        ))
        outcome, reasoner = self.run_turn(
            AgentDecision(
                response="I deleted four.",
                claimed_completed_actions=("ok-1", "ok-2", "ok-3", "ok-4"),
            ),
            AgentDecision(
                response="I deleted three.",
                claimed_completed_actions=("ok-1", "ok-2", "ok-3"),
            ),
            state=state, steps=2,
        )
        self.assertEqual(outcome.response, "I deleted three.")
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"],
            "claimed_action_not_attempted",
        )

    def test_calling_a_success_unfinished_is_refused(self) -> None:
        """Understating what happened to the world is its own untruth."""
        state = goal(attempts=(attempt("call-1"),))
        outcome, reasoner = self.run_turn(
            AgentDecision(
                response="I could not delete it.",
                unfinished_actions=("call-1",),
            ),
            AgentDecision(
                response="It is deleted.",
                claimed_completed_actions=("call-1",),
            ),
            state=state, steps=2,
        )
        self.assertEqual(outcome.response, "It is deleted.")
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"],
            "unfinished_action_actually_succeeded",
        )

    def test_a_failed_action_may_be_reported_as_unfinished(self) -> None:
        """"I couldn't delete those emails." stays valid."""
        state = goal(attempts=(
            attempt("call-1", state=CapabilityResultState.FAILED),
        ))
        outcome, _ = self.run_turn(
            AgentDecision(
                response="I could not delete those emails.",
                unfinished_actions=("call-1",),
            ),
            state=state,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "I could not delete those emails.")

    def test_a_search_never_needs_accounting_for(self) -> None:
        """A non-mutating attempt does not put the response under the rule."""
        state = goal(attempts=(
            attempt("call-search", capability_id="ask_web_search"),
        ))
        outcome, _ = self.run_turn(
            AgentDecision(response="I found what you asked about."),
            state=state,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)


class OrdinaryConversationIsUnaffected(Harness):
    """6. The rule constrains claims of completion and nothing else."""

    def test_an_ordinary_response_claiming_nothing_is_untouched(self) -> None:
        outcome, _ = self.run_turn(
            AgentDecision(response="The part arrives on Thursday."),
            state=goal(),
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "The part arrives on Thursday.")

    def test_saying_what_she_is_about_to_do_needs_no_evidence(self) -> None:
        """Explicitly permitted: intention is not a completion claim."""
        outcome, _ = self.run_turn(
            AgentDecision(
                response="I found the emails and I am trying to delete them.",
            ),
            state=goal(),
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertIn("trying to delete", outcome.response)

    def test_a_goalless_conversational_turn_is_untouched(self) -> None:
        outcome, _ = self.run_turn(
            AgentDecision(response="I think the regulator is the wrong part."),
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)

    def test_reporting_a_failure_needs_no_proof_but_must_account(self) -> None:
        """"I couldn't delete those emails." stays valid, and stays accounted.

        Naming the call as unfinished asserts nothing about the world and
        needs no proof. It is required only because this turn recorded a
        mutation attempt, and a response that mentions none of the record is
        how a partial failure gets quietly dropped.
        """
        state = goal(attempts=(attempt(
            "call-trash-1", state=CapabilityResultState.FAILED,
        ),))
        outcome, _ = self.run_turn(
            AgentDecision(
                response="I could not delete that one; it failed.",
                unfinished_actions=("call-trash-1",),
            ),
            state=state,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(
            outcome.response, "I could not delete that one; it failed."
        )


class TheContractHoldsTheRule(unittest.TestCase):
    """The declaration is structural, not prose a model may ignore."""

    def test_only_a_response_may_claim_a_completed_action(self) -> None:
        with self.assertRaises(ValueError):
            AgentDecision(
                call=CapabilityCall("call-1", "move_mail_message_to_trash", {}),
                claimed_completed_actions=("call-1",),
            )
        with self.assertRaises(ValueError):
            AgentDecision(
                finish_silently=True, claimed_completed_actions=("call-1",),
            )

    def test_a_claim_cannot_repeat_a_call_identifier(self) -> None:
        with self.assertRaises(ValueError):
            AgentDecision(
                response="Done.", claimed_completed_actions=("call-1", "call-1"),
            )

    def test_a_mutating_capability_must_be_effectful(self) -> None:
        """Changing the world outside AL/X is an effect by definition."""
        with self.assertRaises(ValueError):
            CapabilityDefinition(
                "impossible", "Mutates without effect", SCHEMA, SCHEMA,
                SideEffect.NONE, externally_observable_mutation=True,
            )

    def test_mutation_is_declared_rather_than_inferred_from_side_effect(self) -> None:
        """A search and a send are both EFFECTFUL; only one changes the world."""
        self.assertIs(SEARCH.side_effect, SideEffect.EFFECTFUL)
        self.assertFalse(SEARCH.externally_observable_mutation)
        self.assertIs(TRASH.side_effect, SideEffect.EFFECTFUL)
        self.assertTrue(TRASH.externally_observable_mutation)

    def test_the_real_mutating_capabilities_declare_it(self) -> None:
        """The live definitions, not stand-ins."""
        from alx.tools.mail import TRASH_DEFINITION, SEND_REPLY_DEFINITION
        from alx.tools.repository import DEFINITION as MERGE_DEFINITION

        for definition in (
            TRASH_DEFINITION, SEND_REPLY_DEFINITION, MERGE_DEFINITION,
        ):
            self.assertTrue(
                definition.externally_observable_mutation,
                f"{definition.capability_id} changes the world outside AL/X",
            )

    def test_reading_and_searching_declare_no_mutation(self) -> None:
        from alx.tools.mail import READ_DEFINITION, SEARCH_DEFINITION
        from alx.tools.web import SEARCH_DEFINITION as WEB_SEARCH

        for definition in (READ_DEFINITION, SEARCH_DEFINITION, WEB_SEARCH):
            self.assertFalse(definition.externally_observable_mutation)


class TheProtocolStatesTheRule(unittest.TestCase):
    def test_the_protocol_states_when_a_completion_may_be_claimed(self) -> None:
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        for stated in (
            "claimed_completed_actions",
            "unfinished_actions",
            "Whether an external action happened is not yours to assert",
            "A turn that dispatched nothing recorded nothing",
            "An intention, a queued action",
            "None of this restricts ordinary speaking",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_decision_schema_carries_the_field(self) -> None:
        import json
        from alx.core.model_reasoner import decision_schema

        variants = decision_schema()["properties"]["action"]["anyOf"]
        responses = [
            item for item in variants
            if item["properties"].get("type", {}).get("const") == "respond"
        ]
        self.assertEqual(len(responses), 1)
        described = responses[0]["properties"]["claimed_completed_actions"]
        self.assertIn("executed and succeeded", json.dumps(described))


if __name__ == "__main__":
    unittest.main()
