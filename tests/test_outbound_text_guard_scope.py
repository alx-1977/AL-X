"""The unheard-text rule applies to sending, and only to sending.

On 2026-09-05 Friedl asked what makes an MP2723A STAT light blink. AL/X ran
one search, found the datasheet was a PDF she cannot read, and reformulated
her search eight times. Every reformulation was refused, because the guard
that stops her sending Friedl wording he has never heard inspects arguments
named `body`, `body_text` or `subject` — and Web Search V1 had named its
argument `subject`. A search phrase is never something she has spoken aloud,
so no search carrying an approval could ever pass. Twenty reasoning calls,
about R11, and she was never told why.

Two things are proved here. That the guard is scoped by what a capability
declares it does rather than by what its arguments are called, so a read
capability cannot be mistaken for a send. And that when deterministic
governance refuses something it already knows the reason for, that reason
reaches her.

The tests run through the real Core dispatch path. Web Search V1's own tests
all passed while this was live, because they call executors directly and
never reach the loop where the guard lives.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision,
    ApprovalProposal,
    ApprovalScope,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    GoalMutationKind,
    GoalProposal,
    SideEffect,
    StructuredSchema,
    SuccessCriterion,
    ValueKind,
)
from alx.core import CoreAgent, CoreState
from alx.core.model_reasoner import _attempt_payload
from alx.goals import SQLiteGoalStore
from alx.tools import ASK_WEB_SEARCH, WEB_SEARCH_DEFINITION
from alx.tools.mail import SEND_MAIL_REPLY, SEND_REPLY_DEFINITION


NOW = datetime(2026, 9, 5, 10, 33, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
_STRING = StructuredSchema(ValueKind.STRING)

# A read capability that happens to name an argument `subject`. Web Search is
# one; this proves the point is general rather than about that capability.
READ_WITH_SUBJECT = CapabilityDefinition(
    "look_up_reference",
    "Look something up. Reads only; sends nothing to anyone.",
    StructuredSchema(ValueKind.OBJECT, {"lookup_id": _STRING, "subject": _STRING},
                     ("lookup_id", "subject"), extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, {"found": _STRING}, ("found",),
                     extra_properties=False),
    SideEffect.EFFECTFUL,
)


class Queued:
    """A fake Core model, following the harness the loop tests already use."""

    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


def conversation(*, spoken: str, asked: str) -> ConversationSnapshot:
    """Her last utterance, then what Friedl said next."""
    return ConversationSnapshot(
        "conversation-1",
        (
            ConversationTurn("conversation-1", "turn-1",
                             ConversationOrigin.ALX_RESPONSE, spoken, NOW, None),
            ConversationTurn("conversation-1", "turn-2", ConversationOrigin.TYPED,
                             asked, NOW, "friedl"),
        ),
        2,
        RETENTION,
    )


class GuardScopeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")
        self.addCleanup(self.store.close)
        self.dispatched: list[str] = []

    def agent(self, reasoner, capabilities, identifiers=("goal-1",)):
        values = iter(identifiers)

        def dispatch(call, state):
            # A controlled stand-in: nothing reaches Brave, a mail server or
            # any other outside system from these tests.
            self.dispatched.append(call.capability_id)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(call.call_id, call.capability_id,
                                 CapabilityResultState.SUCCEEDED, {"ok": True}),
            )

        return CoreAgent(
            self.store, reasoner, dispatch, capabilities,
            clock=lambda: NOW, identifier_factory=lambda: next(values),
        )

    @staticmethod
    def approved(call: CapabilityCall) -> ApprovalProposal:
        return ApprovalProposal(
            call.approval_id,
            ApprovalScope(call.capability_id, call.arguments),
            "turn:turn-2",
        )

    def goal(self) -> GoalProposal:
        return GoalProposal(
            GoalMutationKind.CREATE, "Answer the question",
            (SuccessCriterion("criterion-1", "answered"),),
        )


class WebSearchIsNotOutboundTests(GuardScopeTestCase):
    """The incident: a search subject she has never spoken must still run."""

    def search_call(self) -> CapabilityCall:
        return CapabilityCall(
            "call-s1", ASK_WEB_SEARCH,
            {"search_id": "s1",
             "subject": "MP2723A STAT pin blinking fault condition datasheet"},
            approval_id="approve-s1",
        )

    def test_a_search_with_an_unspoken_subject_reaches_dispatch(self) -> None:
        call = self.search_call()
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="The STAT LED blinks on a charging fault."),
        )
        outcome = self.agent(reasoner, (WEB_SEARCH_DEFINITION,)).process(
            conversation(spoken="Shall I trash the confirmation?",
                         asked="what makes the MP2723A STAT light blink?"),
            RETENTION, 4,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.dispatched, [ASK_WEB_SEARCH],
                         "the search never reached dispatch")

    def test_no_attempt_is_refused_for_unheard_text(self) -> None:
        call = self.search_call()
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="Answered."),
        )
        outcome = self.agent(reasoner, (WEB_SEARCH_DEFINITION,)).process(
            conversation(spoken="Shall I trash the confirmation?",
                         asked="what makes the STAT light blink?"),
            RETENTION, 4,
        )
        reasons = [
            item.reason_code for item in outcome.snapshot.state.attempts
            if item.disposition is CapabilityAttemptDisposition.REJECTED
        ]
        self.assertNotIn("approval_covers_unheard_text", reasons)

    def test_reformulating_a_search_is_not_refused_either(self) -> None:
        """Eight reformulations were refused during the incident."""
        calls = [
            CapabilityCall(f"call-s{index}", ASK_WEB_SEARCH,
                           {"search_id": f"s{index}",
                            "subject": f"MP2723A STAT blink phrasing {index}"},
                           approval_id=f"approve-s{index}")
            for index in range(1, 4)
        ]
        reasoner = Queued(
            AgentDecision(call=calls[0], approval_proposal=self.approved(calls[0]),
                          goal_proposal=self.goal()),
            *[AgentDecision(call=item, approval_proposal=self.approved(item))
              for item in calls[1:]],
            AgentDecision(response="Answered."),
        )
        self.agent(reasoner, (WEB_SEARCH_DEFINITION,)).process(
            conversation(spoken="Shall I trash the confirmation?",
                         asked="what makes the STAT light blink?"),
            RETENTION, 6,
        )
        self.assertEqual(self.dispatched, [ASK_WEB_SEARCH] * 3)

    def test_the_declaration_is_what_scopes_it(self) -> None:
        self.assertFalse(WEB_SEARCH_DEFINITION.transmits_authored_text)
        self.assertIn("subject", WEB_SEARCH_DEFINITION.input_schema.properties)


class OutboundTextIsStillProtectedTests(GuardScopeTestCase):
    """The safety boundary the guard exists for is unchanged."""

    def send_call(self, body: str) -> CapabilityCall:
        return CapabilityCall(
            "call-m1", SEND_MAIL_REPLY,
            {"mailbox_id": "INBOX", "uid_validity": "1", "uid": "2",
             "to": ["john@example.test"], "subject": "Re: parts",
             "body": body},
            approval_id="approve-m1",
        )

    def test_unheard_mail_text_is_still_refused(self) -> None:
        call = self.send_call("Wording Friedl has never heard me say.")
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="I could not send that."),
        )
        self.agent(reasoner, (SEND_REPLY_DEFINITION,)).process(
            conversation(spoken="Shall I reply to John?", asked="yes please"),
            RETENTION, 4,
        )
        # The safety property: nothing left the system. Whether she is *told*
        # is covered separately, and see the note on
        # `test_a_refusal_before_the_goal_commits_is_not_recorded`.
        self.assertEqual(self.dispatched, [], "unheard wording was transmitted")

    def test_an_unheard_subject_line_is_still_refused(self) -> None:
        """A subject is authored text too, when the capability sends it."""
        call = CapabilityCall(
            "call-m1", SEND_MAIL_REPLY,
            {"mailbox_id": "INBOX", "uid_validity": "1", "uid": "2",
             "to": ["john@example.test"],
             "subject": "A subject line he never heard",
             "body": "The parts arrive Tuesday."},
            approval_id="approve-m1",
        )
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="I could not send that."),
        )
        self.agent(reasoner, (SEND_REPLY_DEFINITION,)).process(
            conversation(spoken="The parts arrive Tuesday.", asked="send it"),
            RETENTION, 4,
        )
        self.assertEqual(self.dispatched, [])

    def test_heard_mail_text_still_sends(self) -> None:
        """The rule constrains sending unheard words, not sending."""
        body = "The parts arrive Tuesday."
        call = self.send_call(body)
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="Sent."),
        )
        outcome = self.agent(reasoner, (SEND_REPLY_DEFINITION,)).process(
            conversation(spoken=f"Re: parts {body}", asked="send it"),
            RETENTION, 4,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.dispatched, [SEND_MAIL_REPLY])

    def test_the_sending_capability_declares_itself(self) -> None:
        self.assertTrue(SEND_REPLY_DEFINITION.transmits_authored_text)


class ArgumentNameIsNotEvidenceTests(GuardScopeTestCase):
    """The general form: `subject` alone never means outbound text."""

    def test_a_read_capability_named_subject_is_not_treated_as_a_send(self) -> None:
        call = CapabilityCall(
            "call-r1", "look_up_reference",
            {"lookup_id": "r1", "subject": "something never spoken aloud"},
            approval_id="approve-r1",
        )
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="Looked it up."),
        )
        outcome = self.agent(reasoner, (READ_WITH_SUBJECT,)).process(
            conversation(spoken="Anything else?", asked="look this up"),
            RETENTION, 4,
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.dispatched, ["look_up_reference"])

    def test_a_read_capability_named_body_is_not_treated_as_a_send(self) -> None:
        definition = CapabilityDefinition(
            "inspect_document", "Inspect a document. Sends nothing.",
            StructuredSchema(ValueKind.OBJECT,
                             {"document_id": _STRING, "body": _STRING},
                             ("document_id", "body"), extra_properties=False),
            StructuredSchema(ValueKind.OBJECT, {"found": _STRING}, ("found",),
                             extra_properties=False),
            SideEffect.EFFECTFUL,
        )
        call = CapabilityCall(
            "call-d1", "inspect_document",
            {"document_id": "d1", "body": "text she never spoke"},
            approval_id="approve-d1",
        )
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="Inspected."),
        )
        self.agent(reasoner, (definition,)).process(
            conversation(spoken="Anything else?", asked="check this"),
            RETENTION, 4,
        )
        self.assertEqual(self.dispatched, ["inspect_document"])

    def test_the_default_is_not_outbound(self) -> None:
        """A capability says it sends; silence is not a declaration."""
        self.assertFalse(READ_WITH_SUBJECT.transmits_authored_text)

    def test_the_declaration_must_be_a_boolean(self) -> None:
        with self.assertRaises(TypeError):
            CapabilityDefinition(
                "x", "y",
                StructuredSchema(ValueKind.OBJECT),
                StructuredSchema(ValueKind.OBJECT),
                SideEffect.NONE,
                transmits_authored_text="yes",
            )


class RejectionReasonReachesCoreTests(GuardScopeTestCase):
    """A refusal we already have a reason for is not delivered mute."""

    def test_the_projection_carries_the_reason_code(self) -> None:
        attempt = CapabilityAttempt(
            CapabilityCall("call-1", SEND_MAIL_REPLY, {"body": "x"}),
            CapabilityAttemptDisposition.REJECTED, False,
            reason_code="approval_covers_unheard_text",
        )
        payload = _attempt_payload(attempt)
        self.assertEqual(payload["disposition"], "rejected")
        self.assertEqual(payload["reason_code"], "approval_covers_unheard_text")

    def test_an_ordinary_attempt_carries_no_reason(self) -> None:
        attempt = CapabilityAttempt(
            CapabilityCall("call-1", ASK_WEB_SEARCH, {"search_id": "s"}),
            CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("call-1", ASK_WEB_SEARCH,
                             CapabilityResultState.SUCCEEDED, {"ok": True}),
        )
        self.assertIsNone(_attempt_payload(attempt)["reason_code"])

    def test_she_sees_the_reason_on_her_next_turn(self) -> None:
        """Through the projection the Core actually receives.

        The goal is committed first, so the refusal has somewhere to live.
        """
        call = CapabilityCall(
            "call-m1", SEND_MAIL_REPLY,
            {"mailbox_id": "INBOX", "uid_validity": "1", "uid": "2",
             "to": ["john@example.test"], "subject": "Re: parts",
             "body": "Wording he never heard."},
            approval_id="approve-m1",
        )
        # The goal is committed on an earlier turn, so the refusal has
        # somewhere durable to live when it happens.
        opening = Queued(
            AgentDecision(response="Working on it.", goal_proposal=self.goal())
        )
        first = self.agent(opening, (SEND_REPLY_DEFINITION,)).process(
            conversation(spoken="Shall I reply?", asked="yes"), RETENTION, 2
        )
        goal_id = first.snapshot.state.goal_id

        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_id=goal_id),
            AgentDecision(response="I could not send that.", goal_id=goal_id),
        )
        self.agent(reasoner, (SEND_REPLY_DEFINITION,), identifiers=()).process(
            conversation(spoken="Shall I reply?", asked="yes"),
            RETENTION, 3,
        )
        from alx.core.model_reasoner import _context_payload

        goal = reasoner.contexts[-1].active_goal
        self.assertIsNotNone(goal, "no goal reached her")
        payload = json.loads(_context_payload(reasoner.contexts[-1]))
        attempts = payload["active_goal"]["attempts"]
        self.assertTrue(
            any(item.get("reason_code") == "approval_covers_unheard_text"
                for item in attempts),
            f"the reason never reached her: {attempts}",
        )

    def test_a_refusal_before_the_goal_commits_is_not_recorded(self) -> None:
        """No durable record, and that is the desired behaviour.

        A refused call arriving in the same decision that proposes the goal
        has no committed snapshot to attach to. Nothing is created to hold it:
        no goal is persisted, no attempt is stored, because nothing was
        dispatched and no external effect occurred. GoalState stays the sole
        owner of durable attempts.

        She is still told. The refusal travels on the transient channel
        instead, which `PreGoalRefusalIsVisibleTests` covers. This test guards
        the other half — that visibility was not bought by writing durable
        state for something that never happened.
        """
        call = CapabilityCall(
            "call-m1", SEND_MAIL_REPLY,
            {"mailbox_id": "INBOX", "uid_validity": "1", "uid": "2",
             "to": ["john@example.test"], "subject": "Re: parts",
             "body": "Wording he never heard."},
            approval_id="approve-m1",
        )
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="I could not send that."),
        )
        self.agent(reasoner, (SEND_REPLY_DEFINITION,)).process(
            conversation(spoken="Shall I reply?", asked="yes"),
            RETENTION, 4,
        )
        self.assertEqual(self.dispatched, [])
        self.assertIsNone(
            reasoner.contexts[-1].active_goal,
            "if this now holds the refusal, the gap is closed and this test "
            "should assert that instead",
        )

    def test_the_instructions_tell_her_to_read_it(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/alx/core/model_reasoner.py"
        ).read_text()
        self.assertIn("carries reason_code", source)


class EveryOutboundCapabilityDeclaresItselfTests(unittest.TestCase):
    """A capability that transmits her wording must say so.

    Structural, and deliberately so: the incident was a mismatch between what
    a capability does and what the runtime could tell about it. A new sending
    capability that forgot to declare itself would silently escape the rule.
    """

    def test_every_capability_carrying_authored_text_is_accounted_for(self) -> None:
        from alx.core.loop import CoreAgent as Agent
        from alx.tools import DEFINITIONS as MAIL, XERO_DEFINITIONS
        from alx.tools.mail import SEND_DEFINITIONS
        from alx.tools.notebook import DEFINITIONS as NOTEBOOK
        from alx.tools.web import DEFINITION as PAGE, SEARCH_DEFINITION

        every = (*MAIL, *SEND_DEFINITIONS, *XERO_DEFINITIONS, *NOTEBOOK,
                 PAGE, SEARCH_DEFINITION)
        declared = {
            item.capability_id for item in every if item.transmits_authored_text
        }
        # Exactly the one capability that sends. If a new sending capability
        # is added, add it here deliberately rather than by accident.
        self.assertEqual(declared, {SEND_MAIL_REPLY})

    def test_a_capability_holding_authored_arguments_is_read_or_declared(self) -> None:
        """The collision itself: holding `subject` is not a declaration."""
        from alx.core.loop import CoreAgent as Agent
        from alx.tools import DEFINITIONS as MAIL, XERO_DEFINITIONS
        from alx.tools.mail import SEND_DEFINITIONS
        from alx.tools.web import DEFINITION as PAGE, SEARCH_DEFINITION

        every = (*MAIL, *SEND_DEFINITIONS, *XERO_DEFINITIONS, PAGE,
                 SEARCH_DEFINITION)
        holding = {
            item.capability_id: item.transmits_authored_text
            for item in every
            if set(item.input_schema.properties) & Agent._AUTHORED_TEXT_ARGUMENTS
        }
        self.assertIn(SEND_MAIL_REPLY, holding)
        self.assertTrue(holding[SEND_MAIL_REPLY])
        for capability_id, declares in holding.items():
            if capability_id != SEND_MAIL_REPLY:
                self.assertFalse(
                    declares,
                    f"{capability_id} holds an authored-text argument and "
                    "declares that it transmits; confirm that is intended",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class PreGoalRefusalIsVisibleTests(GuardScopeTestCase):
    """A refusal before the goal commits still reaches her.

    When one decision proposes a goal and an action together, the goal has not
    committed when the action is refused, so there is no durable state to
    append the refusal to and it used to be dropped. Nothing unsafe happened —
    nothing was dispatched — but she reasoned on knowing only that something
    had been rejected. Ten such refusals cost eleven reasoning calls and told
    her nothing.

    The refusal now travels on the transient channel the dispatch-blocked path
    beside it already used. No goal is created to hold it, and nothing durable
    is written, because nothing happened.
    """

    def refused_send(self, index: int = 1) -> CapabilityCall:
        return CapabilityCall(
            f"call-m{index}", SEND_MAIL_REPLY,
            {"mailbox_id": "INBOX", "uid_validity": "1", "uid": "2",
             "to": ["john@example.test"], "subject": "Re: parts",
             "body": f"Wording he never heard {index}."},
            approval_id=f"approve-m{index}",
        )

    def run_once(self, *decisions, capabilities=None, budget=4, identifiers=("goal-1",)):
        reasoner = Queued(*decisions)
        outcome = self.agent(
            reasoner, capabilities or (SEND_REPLY_DEFINITION,),
            identifiers=identifiers,
        ).process(
            conversation(spoken="Shall I reply?", asked="yes"), RETENTION, budget
        )
        return reasoner, outcome

    def test_the_refusal_becomes_transient_feedback(self) -> None:
        call = self.refused_send()
        reasoner, _ = self.run_once(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="I could not send that."),
        )
        refused = list(reasoner.contexts[-1].refused_calls)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["call_id"], "call-m1")
        self.assertEqual(refused[0]["capability_id"], SEND_MAIL_REPLY)
        self.assertEqual(refused[0]["reason"], "approval_covers_unheard_text")

    def test_nothing_is_dispatched(self) -> None:
        call = self.refused_send()
        self.run_once(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="I could not send that."),
        )
        self.assertEqual(self.dispatched, [], "a refused action reached dispatch")

    def test_the_refusal_reaches_the_payload_core_receives(self) -> None:
        """Through the projection itself, not the context before it."""
        from alx.core.model_reasoner import _context_payload

        call = self.refused_send()
        reasoner, _ = self.run_once(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="I could not send that."),
        )
        payload = json.loads(_context_payload(reasoner.contexts[-1]))
        self.assertEqual(
            payload["refused_calls"],
            [{"call_id": "call-m1", "capability_id": SEND_MAIL_REPLY,
              "reason": "approval_covers_unheard_text",
              # The refusal is scoped by reason and subject, so one corrected
              # refusal cannot swallow the next unrelated one.
              "subject": SEND_MAIL_REPLY}],
        )

    def test_no_goal_and_no_durable_attempt_are_created(self) -> None:
        """Transient feedback, not execution history. Nothing happened."""
        call = self.refused_send()
        reasoner, _ = self.run_once(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="I could not send that."),
        )
        self.assertIsNone(reasoner.contexts[-1].active_goal)
        self.assertEqual(
            len(self.store.list_goals()), 0,
            "a goal was persisted merely to hold a refusal",
        )

    def test_a_post_goal_refusal_is_still_durable(self) -> None:
        """Where a goal exists, the attempt is recorded on it exactly as before."""
        opening = Queued(
            AgentDecision(response="Working on it.", goal_proposal=self.goal())
        )
        first = self.agent(opening, (SEND_REPLY_DEFINITION,)).process(
            conversation(spoken="Shall I reply?", asked="yes"), RETENTION, 2
        )
        goal_id = first.snapshot.state.goal_id

        call = self.refused_send()
        reasoner = Queued(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_id=goal_id),
            AgentDecision(response="I could not send that.", goal_id=goal_id),
        )
        self.agent(reasoner, (SEND_REPLY_DEFINITION,), identifiers=()).process(
            conversation(spoken="Shall I reply?", asked="yes"), RETENTION, 3
        )
        goal = reasoner.contexts[-1].active_goal
        self.assertIsNotNone(goal)
        rejected = [
            item for item in goal.attempts
            if item.disposition is CapabilityAttemptDisposition.REJECTED
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].reason_code, "approval_covers_unheard_text")

    def test_repeated_refusals_no_longer_consume_the_budget(self) -> None:
        """The incident shape: ten refusals used to buy eleven reasoning calls.

        One refusal is explained; a second ends the turn with its reason. She
        keeps a full step to choose something else, and loses only the steps
        she would have spent against a wall that cannot move.
        """
        decisions = []
        for index in range(1, 11):
            call = self.refused_send(index)
            decisions.append(
                AgentDecision(call=call, approval_proposal=self.approved(call),
                              goal_proposal=self.goal())
            )
        decisions.append(AgentDecision(response="giving up"))

        reasoner, outcome = self.run_once(
            *decisions, budget=12,
            identifiers=tuple(f"goal-{index}" for index in range(30)),
        )
        self.assertLessEqual(
            len(reasoner.contexts), 3,
            f"the turn still spent {len(reasoner.contexts)} reasoning calls",
        )
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "approval_covers_unheard_text")
        self.assertEqual(self.dispatched, [])
        self.assertEqual(len(self.store.list_goals()), 0)

    def test_she_may_still_do_something_different_after_a_refusal(self) -> None:
        """The stop prevents pointless repetition, never recovery."""
        refused = self.refused_send()
        alternative = CapabilityCall(
            "call-s1", ASK_WEB_SEARCH,
            {"search_id": "s1", "subject": "something else entirely"},
        )
        reasoner, outcome = self.run_once(
            AgentDecision(call=refused, approval_proposal=self.approved(refused),
                          goal_proposal=self.goal()),
            AgentDecision(call=alternative, goal_proposal=self.goal()),
            AgentDecision(response="I did the other thing instead."),
            capabilities=(SEND_REPLY_DEFINITION, WEB_SEARCH_DEFINITION),
            budget=5, identifiers=("goal-1", "goal-2", "goal-3"),
        )
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.dispatched, [ASK_WEB_SEARCH],
                         "a viable alternative was blocked")

    def test_a_crash_before_the_next_step_loses_nothing_that_matters(self) -> None:
        """The refusal is feedback, not history: nothing needs recovering."""
        call = self.refused_send()
        self.run_once(
            AgentDecision(call=call, approval_proposal=self.approved(call),
                          goal_proposal=self.goal()),
            AgentDecision(response="I could not send that."),
        )
        self.store.close()
        reopened = SQLiteGoalStore(
            Path(self.directory.name) / "goals.sqlite3"
        )
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.list_goals(), ())
        self.assertEqual(self.dispatched, [])
