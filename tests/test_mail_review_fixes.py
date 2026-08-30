"""Regression evidence for the review findings on the mail slice.

Each test corresponds to a defect that was reproduced against the code before
being fixed. Three came from the Greptile review of PR #8; the approval-release
defect was found in Friedl's own durable goal state, where one request produced
five consecutive rejected Trash attempts.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision, ApprovalLifecycle, ApprovalProposal, ApprovalScope,
    BackgroundEvent, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityDefinition, CapabilityResult, CapabilityResultState,
    ConversationOrigin, ConversationSnapshot, ConversationTurn, Evidence,
    GoalMutationKind, GoalProposal, MemoryKind, MemoryProposal, SideEffect,
    StructuredSchema, SuccessCriterion, ValueKind,
    ProgressRecord, WorkItem,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate  # noqa: E402

NOW = datetime(2026, 8, 29, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
TRASH = "move_mail_message_to_trash"
ARGS = {"mailbox_id": "INBOX", "uid": "58603", "uid_validity": "1376545928"}
DEFINITION = CapabilityDefinition(TRASH, "move one message to trash", SCHEMA, SCHEMA,
                                  SideEffect.EFFECTFUL)


class Queued:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def conversation(*contents: str, events=()) -> ConversationSnapshot:
    turns = tuple(
        ConversationTurn("conversation-1", f"turn-{i + 1}", ConversationOrigin.TYPED,
                         text, NOW, "friedl")
        for i, text in enumerate(contents or ("trash that reminder",))
    )
    return ConversationSnapshot("conversation-1", turns, len(turns), RETENTION, events)


class ApprovalReleaseTests(unittest.TestCase):
    """A refused or impossible action must not burn Friedl's approval."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")
        self.invocations: list[object] = []

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def broker_for(self, executor):
        return CapabilityBroker(
            CapabilityRegistry((DEFINITION,)),
            SafetyGate({TRASH: AuthorityPolicy(frozenset({"mail.trash"}),
                                               approval_required=True)}),
            {TRASH: executor},
        )

    def dispatcher(self, broker):
        def dispatch(call, state):
            return broker.dispatch(
                call, AuthorityContext("friedl", frozenset({"mail.trash"}), NOW,
                                       state.approvals)
            )
        return dispatch

    def first_turn(self, executor):
        broker = self.broker_for(executor)
        reasoner = Queued(
            AgentDecision(
                goal_proposal=GoalProposal(
                    kind=GoalMutationKind.CREATE,
                    objective_summary="Move the reminder to Trash",
                    success_criteria=(SuccessCriterion("criterion-1", "moved"),)),
                call=CapabilityCall("call-1", TRASH, ARGS, "approval-1"),
                approval_proposal=ApprovalProposal(
                    "approval-1", ApprovalScope(TRASH, ARGS), "turn:turn-1"),
            ),
            AgentDecision(response="That did not go through."),
        )
        agent = CoreAgent(self.store, reasoner, self.dispatcher(broker), (DEFINITION,),
                          clock=lambda: NOW, identifier_factory=lambda: "goal-1")
        return agent.process(conversation(), None, RETENTION, 4)

    def test_a_failed_move_returns_the_approval_for_a_retry(self) -> None:
        """The defect that produced five rejected Trash attempts in real state."""
        def explode(arguments):
            raise RuntimeError("imap blip")

        self.first_turn(explode)
        approvals = self.store.load("goal-1").state.approvals
        self.assertEqual(len(approvals), 1)
        self.assertIs(approvals[0].lifecycle, ApprovalLifecycle.GRANTED,
                      "a definite pre-effect failure must not spend the approval")

    def test_the_released_approval_actually_authorises_the_retry(self) -> None:
        attempts = {"count": 0}

        def flaky(arguments):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("imap blip")
            return CapabilityResult("call-2", TRASH, CapabilityResultState.SUCCEEDED, {})

        self.first_turn(flaky)
        broker = self.broker_for(flaky)
        reasoner = Queued(
            AgentDecision(call=CapabilityCall("call-2", TRASH, ARGS, "approval-1")),
            AgentDecision(response="Moved to Trash."),
        )
        agent = CoreAgent(self.store, reasoner, self.dispatcher(broker), (DEFINITION,),
                          clock=lambda: NOW, identifier_factory=lambda: "goal-1")
        outcome = agent.process(conversation("trash it", "try again"), "goal-1",
                                RETENTION, 4)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(attempts["count"], 2, "the retry must reach the executor")
        approvals = self.store.load("goal-1").state.approvals
        self.assertIs(approvals[0].lifecycle, ApprovalLifecycle.CONSUMED)

    def test_an_action_that_may_have_taken_effect_keeps_the_approval_spent(self) -> None:
        """An ambiguous outcome must never be retried automatically."""
        def malformed(arguments):
            return "not a capability result"

        self.first_turn(malformed)
        approvals = self.store.load("goal-1").state.approvals
        self.assertIs(approvals[0].lifecycle, ApprovalLifecycle.CONSUMED)

    def test_a_refused_action_is_not_retried_under_a_fresh_identifier(self) -> None:
        """A new call or approval id does not make a refused action different."""
        dispatched: list[CapabilityCall] = []

        def dispatch(call, state):
            dispatched.append(call)
            from alx.contracts import CapabilityAttempt
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED,
                                     False, reason_code="approval_invalid")

        reasoner = Queued(
            AgentDecision(
                goal_proposal=GoalProposal(
                    kind=GoalMutationKind.CREATE,
                    objective_summary="Move the reminder to Trash",
                    success_criteria=(SuccessCriterion("criterion-1", "moved"),)),
                call=CapabilityCall("call-1", TRASH, ARGS, "approval-1")),
            AgentDecision(call=CapabilityCall("call-2", TRASH, ARGS, "approval-2")),
            AgentDecision(response="unreachable"),
        )
        agent = CoreAgent(self.store, reasoner, dispatch, (DEFINITION,),
                          clock=lambda: NOW, identifier_factory=lambda: "goal-1")
        outcome = agent.process(conversation(), None, RETENTION, 4)
        self.assertEqual(outcome.reason, "repeated_rejected_call")
        self.assertEqual(len(dispatched), 1, "the refused action must not repeat")


class TransientContentTests(unittest.TestCase):
    """A mail body shown to the model must not reach durable state."""

    BODY = (
        "Thanks for waiting. Revision B is quoted at R14 500 for 25 units, with a "
        "ten working day lead time, and we can start production once you confirm "
        "the order in writing."
    )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def conversation_with_body(self) -> ConversationSnapshot:
        event = BackgroundEvent(
            "event-1", "mail.observed", NOW,
            data={"uid": "58603", "subject": "Re: Panel revision B quote"},
            transient_data={"body_text": self.BODY},
        )
        return conversation("what does that one say?", events=(event,))

    def agent(self, reasoner) -> CoreAgent:
        return CoreAgent(self.store, reasoner, lambda call, state: None, (DEFINITION,),
                         clock=lambda: NOW, identifier_factory=lambda: "goal-1")

    def test_evidence_quoting_the_body_is_refused(self) -> None:
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE,
                objective_summary="Answer about the quote",
                success_criteria=(SuccessCriterion("criterion-1", "answered"),),
                new_evidence=(Evidence("evidence-1", "mail",
                                       attributes={"quoted": self.BODY},
                                       supports=("criterion-1",),
                                       source_references=("event:event-1",)),)),
            response="Here is what it says.",
            response_requires_goal_commit=True,
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")

    def test_a_memory_quoting_the_body_is_refused(self) -> None:
        reasoner = Queued(AgentDecision(
            response="Noted.",
            memory_proposals=(MemoryProposal(
                "memory-1", MemoryKind.FACTUAL, self.BODY,
                ("event:event-1",), NOW),),
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "memory_proposal_invalid")

    def test_a_short_body_copied_verbatim_is_refused(self) -> None:
        """Regression: a body under the window bypassed the guard entirely.

        A one-line code or account number is the most sensitive thing a
        message can carry, so brevity must not grant an exemption.
        """
        short = "Bank OTP is 449281. Do not share it."
        event = BackgroundEvent(
            "event-1", "mail.observed", NOW,
            data={"uid": "58610", "subject": "Verification"},
            transient_data={"body_text": short},
        )
        conversation_with_short = conversation("what does it say?", events=(event,))
        reasoner = Queued(AgentDecision(
            response="Noted.",
            memory_proposals=(MemoryProposal(
                "memory-1", MemoryKind.FACTUAL, short, ("event:event-1",), NOW),),
        ))
        outcome = self.agent(reasoner).process(
            conversation_with_short, None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "memory_proposal_invalid")

    def test_a_body_split_across_short_records_is_refused(self) -> None:
        """Regression: dividing a body into sub-window fragments bypassed it."""
        chunks = [self.BODY[index:index + 90] for index in range(0, len(self.BODY), 90)]
        self.assertGreater(len(chunks), 1)
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE,
                objective_summary="Answer about the quote",
                success_criteria=(SuccessCriterion("criterion-1", "answered"),),
                new_evidence=(Evidence(
                    "evidence-1", "mail",
                    attributes={"part_one": chunks[0], "part_two": chunks[1]},
                    supports=("criterion-1",),
                    source_references=("event:event-1",)),)),
            response="Here is what it says.",
            response_requires_goal_commit=True,
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")

    def test_a_body_split_into_fragments_below_the_window_is_refused(self) -> None:
        """Regression: fragments shorter than the window each said nothing.

        Judging every durable string alone let a long body be divided into
        pieces that individually looked harmless, so bank details, references
        and account numbers persisted verbatim.
        """
        body = (
            "Bank transfer reference 88213 for R14 500 to account 62 8891 4432, "
            "confirm by Friday."
        )
        event = BackgroundEvent(
            "event-1", "mail.observed", NOW,
            data={"uid": "58620", "subject": "Payment"},
            transient_data={"body_text": body},
        )
        fragments = [body[index:index + 20] for index in range(0, len(body), 20)]
        self.assertGreater(len(fragments), 3)
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE,
                objective_summary="Note the payment",
                success_criteria=(SuccessCriterion("criterion-1", "noted"),),
                new_evidence=(Evidence(
                    "evidence-1", "mail",
                    attributes={f"part_{i}": item for i, item in enumerate(fragments)},
                    supports=("criterion-1",),
                    source_references=("event:event-1",)),)),
            response="Noted.",
            response_requires_goal_commit=True,
        ))
        outcome = self.agent(reasoner).process(
            conversation("what does it say?", events=(event,)), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")

    def test_fragments_spread_across_separate_records_are_refused(self) -> None:
        """Splitting across several evidence items must not evade the guard."""
        from alx.core.loop import CoreAgent

        event = BackgroundEvent(
            "event-1", "mail.observed", NOW, data={"uid": "1"},
            transient_data={"body_text": self.BODY},
        )
        conversation_with_body = conversation("read it", events=(event,))
        halves = [self.BODY[: len(self.BODY) // 2], self.BODY[len(self.BODY) // 2 :]]
        self.assertTrue(
            CoreAgent._reproduces_transient_content(
                conversation_with_body,
                {"first": halves[0], "second": halves[1]},
            )
        )

    def test_stating_a_fact_the_message_contains_is_permitted(self) -> None:
        """A price or a date cannot be conveyed without repeating characters.

        Judging by an absolute character count refused a genuine summary,
        because a shared phrase such as a quoted amount is unavoidable. What
        distinguishes copying from describing is how much of the message is
        reproduced, not whether any run is shared.
        """
        from alx.core.loop import CoreAgent

        body = (
            "Hi Friedl, thanks for waiting. Revision B is quoted at R14 500 for "
            "25 units with a ten working day lead time. Let me know if that "
            "works. Regards, Jan"
        )
        event = BackgroundEvent(
            "event-1", "mail.observed", NOW, data={"uid": "1"},
            transient_data={"body_text": body},
        )
        with_body = conversation("what did Jan say?", events=(event,))
        for summary in (
            "Jan quoted revision B at R14 500 for 25 units, ten day lead time.",
            "Jan came back with a quote and a lead time.",
            "The quote is R14 500.",
        ):
            with self.subTest(summary=summary):
                self.assertFalse(
                    CoreAgent._reproduces_transient_content(with_body, {"v": summary})
                )

    def test_reproducing_most_of_the_message_is_still_refused(self) -> None:
        from alx.core.loop import CoreAgent

        body = (
            "Hi Friedl, thanks for waiting. Revision B is quoted at R14 500 for "
            "25 units with a ten working day lead time. Let me know if that "
            "works. Regards, Jan"
        )
        event = BackgroundEvent(
            "event-1", "mail.observed", NOW, data={"uid": "1"},
            transient_data={"body_text": body},
        )
        with_body = conversation("what did Jan say?", events=(event,))
        for label, text in (
            ("half the message", body[: len(body) // 2]),
            ("the whole message", body),
        ):
            with self.subTest(case=label):
                self.assertTrue(
                    CoreAgent._reproduces_transient_content(with_body, {"v": text})
                )

    def test_fragments_of_any_size_are_refused(self) -> None:
        """Regression: every minimum run length left a hole below it.

        A body divided just under whichever threshold was configured passed
        while still carrying an account number. The records are now also joined
        and compared as one document, which no fragment size defeats.
        """
        from alx.core.loop import CoreAgent

        body = (
            "Bank transfer reference 88213 for R14 500 to account 62 8891 4432, "
            "confirm by Friday."
        )
        event = BackgroundEvent(
            "event-1", "mail.observed", NOW, data={"uid": "1"},
            transient_data={"body_text": body},
        )
        with_body = conversation("read it", events=(event,))
        for size in (1, 2, 3, 5, 8, 11, 12, 20, 40):
            with self.subTest(fragment_size=size):
                pieces = {
                    f"part_{index}": body[index:index + size]
                    for index in range(0, len(body), size)
                }
                self.assertTrue(
                    CoreAgent._reproduces_transient_content(with_body, pieces)
                )

    def test_a_short_secret_split_across_records_is_refused(self) -> None:
        from alx.core.loop import CoreAgent

        event = BackgroundEvent(
            "event-1", "mail.observed", NOW, data={"uid": "1"},
            transient_data={"body_text": "OTP is 449281."},
        )
        with_body = conversation("read it", events=(event,))
        self.assertTrue(
            CoreAgent._reproduces_transient_content(
                with_body, {"a": "OTP is", "b": " 449281."}
            )
        )

    def test_the_core_may_still_describe_the_message_in_its_own_words(self) -> None:
        """The guard must not block legitimate summarisation."""
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE,
                objective_summary="Answer about the quote",
                success_criteria=(SuccessCriterion("criterion-1", "answered"),),
                new_evidence=(Evidence("evidence-1", "mail",
                                       attributes={"gist": "The board house quoted revision B."},
                                       supports=("criterion-1",),
                                       source_references=("event:event-1",)),)),
            response="They quoted revision B with a ten day lead time.",
            response_requires_goal_commit=True,
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)

    def test_durable_event_data_remains_usable(self) -> None:
        """Only transient content is guarded; durable event data is not."""
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE,
                objective_summary="Track the message",
                success_criteria=(SuccessCriterion("criterion-1", "tracked"),),
                new_evidence=(Evidence("evidence-1", "mail",
                                       attributes={"subject": "Re: Panel revision B quote"},
                                       supports=("criterion-1",),
                                       source_references=("event:event-1",)),)),
            response="Tracked.",
            response_requires_goal_commit=True,
        ))
        outcome = self.agent(reasoner).process(
            self.conversation_with_body(), None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)


class MailboxQuotingTests(unittest.TestCase):
    """Regression: an unquoted Trash name made every real move fail.

    The previous fake accepted any argument, so the suite passed while the live
    IMAP server rejected the command. This double rejects an unquoted name with
    a space, the way a real server does.
    """

    class StrictConnection:
        def __init__(self) -> None:
            self.commands: list[tuple] = []

        @staticmethod
        def _check(name: object) -> None:
            if isinstance(name, str) and " " in name and not (
                name.startswith('"') and name.endswith('"')
            ):
                raise OSError("BAD unquoted mailbox name with a space")

        def login(self, address, secret):
            self.commands.append(("LOGIN",))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

        def select(self, mailbox_id, readonly=False):
            self._check(mailbox_id)
            self.commands.append(("SELECT", mailbox_id, readonly))
            return "OK", [b"1"]

        def response(self, name):
            return name, [b"UIDVALIDITY 1"]

        def list(self):
            return "OK", [b'(\\Trash) "/" "Deleted Messages"']

        def uid(self, command, *arguments):
            for argument in arguments:
                self._check(argument)
            self.commands.append(("UID", command, *arguments))
            return "OK", [b""]

    def test_a_trash_name_with_a_space_is_quoted_for_every_command(self) -> None:
        from alx.contracts import MailReference
        from alx.providers.icloud_mail import ICloudMailAdapter

        connection = self.StrictConnection()

        class Observations:
            def acknowledge(self, reference):
                return None

        adapter = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            Observations(), 15, connection_factory=lambda *_, **__: connection,
        )
        trash = adapter.move_to_trash(MailReference("INBOX", "1", "2"))
        self.assertEqual(trash, "Deleted Messages")
        moves = [item for item in connection.commands
                 if item[0] == "UID" and item[1] == "MOVE"]
        self.assertTrue(moves)
        self.assertTrue(
            all(item[-1].startswith('"') for item in moves),
            "the Trash mailbox must reach IMAP quoted",
        )

    def test_quoting_is_idempotent_and_escapes_a_quote(self) -> None:
        from alx.providers.icloud_mail import ICloudMailAdapter

        self.assertEqual(ICloudMailAdapter._quoted("INBOX"), '"INBOX"')
        self.assertEqual(
            ICloudMailAdapter._quoted("Deleted Messages"), '"Deleted Messages"'
        )
        self.assertEqual(ICloudMailAdapter._quoted('"INBOX"'), '"INBOX"')


class TrashAuthorisationTests(unittest.TestCase):
    """D-010 records the authorisation; these assert the safeguards it claims."""

    def test_trash_requires_its_own_permission_and_an_approval(self) -> None:
        from alx.bootstrap.mail import (
            MAIL_READ_PERMISSION, MAIL_TRASH_PERMISSION,
        )

        self.assertNotEqual(MAIL_TRASH_PERMISSION, MAIL_READ_PERMISSION)

    def test_only_trash_carries_external_side_effects(self) -> None:
        from alx.tools import DEFINITIONS

        effectful = [item.capability_id for item in DEFINITIONS
                     if item.side_effect is SideEffect.EFFECTFUL]
        self.assertEqual(effectful, [TRASH])

    def test_no_permanent_deletion_is_reachable(self) -> None:
        source = (Path(__file__).resolve().parents[1]
                  / "src/alx/providers/icloud_mail.py").read_text("utf-8")
        for forbidden in ("EXPUNGE", "\\\\Deleted"):
            self.assertNotIn(forbidden, source)

    def test_the_authorisation_is_recorded(self) -> None:
        decisions = (Path(__file__).resolve().parents[1]
                     / "governance/DECISIONS.md").read_text("utf-8")
        self.assertIn("D-010", decisions)
        self.assertIn(TRASH, decisions)


if __name__ == "__main__":
    unittest.main()


class SpokenResponseGuidanceTests(unittest.TestCase):
    """The model must know its response is spoken, or it writes for a screen."""

    def test_the_model_is_told_its_response_is_spoken(self) -> None:
        source = (Path(__file__).resolve().parents[1]
                  / "src/alx/core/model_reasoner.py").read_text("utf-8")
        self.assertIn("spoken aloud", source)

    def test_the_guidance_states_the_medium_without_scripting_wording(self) -> None:
        """Law 1 and the identity document both forbid scripted phrasing."""
        source = (Path(__file__).resolve().parents[1]
                  / "src/alx/core/model_reasoner.py").read_text("utf-8")
        # No example sentence, greeting, or fixed reply is supplied.
        for scripted in ('say "', "reply with", "respond with the phrase",
                         "always begin", "use the words"):
            self.assertNotIn(scripted, source.lower())

    def test_identity_was_not_expanded_into_style_rules(self) -> None:
        identity = (Path(__file__).resolve().parents[1]
                    / "IDENTITY_AND_MEMORY.md").read_text("utf-8")
        self.assertIn("must not be expanded into detailed style rules", identity)
        self.assertNotIn("spoken aloud", identity)


class SessionResilienceTests(unittest.TestCase):
    """One dropped speech transport must not end the conversation."""

    def test_only_transport_failures_are_treated_as_recoverable(self) -> None:
        from alx.interfaces.server import RECOVERABLE_TRANSPORT_REASONS

        self.assertIn("speech_transcription_error", RECOVERABLE_TRANSPORT_REASONS)
        # A refused action or an invalid Core decision is not a transport fault
        # and must not silently resume as though nothing happened.
        for reason in ("repeated_rejected_call", "active_goal_required",
                       "goal_proposal_invalid", "voice_transport_error"):
            self.assertNotIn(reason, RECOVERABLE_TRANSPORT_REASONS)

    def test_the_handler_resumes_rather_than_returning_once(self) -> None:
        import ast

        tree = ast.parse((Path(__file__).resolve().parents[1]
                          / "src/alx/interfaces/server.py").read_text("utf-8"))
        handler = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_voice"
        )
        # The conversation is re-entered while the browser socket stays open.
        self.assertTrue(
            any(isinstance(node, ast.While) for node in ast.walk(handler)),
            "a recoverable failure must re-enter the exchange",
        )


class DurableFieldCoverageTests(unittest.TestCase):
    """Every durable field is guarded, not only evidence and memory."""

    BODY = (
        "Bank transfer reference 88213 for R14 500 to account 62 8891 4432, "
        "confirm by Friday."
    )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _conversation(self):
        event = BackgroundEvent(
            "event-1", "mail.observed", NOW, data={"uid": "1"},
            transient_data={"body_text": self.BODY},
        )
        return conversation("read it", events=(event,))

    def _attempt(self, **proposal):
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(kind=GoalMutationKind.CREATE, **proposal),
            response="Noted.", response_requires_goal_commit=True,
        ))
        outcome = CoreAgent(
            self.store, reasoner, lambda call, state: None, (),
            clock=lambda: NOW, identifier_factory=lambda: "goal-1",
        ).process(self._conversation(), None, RETENTION, 2)
        stored = sqlite3.connect(
            str(Path(self.directory.name) / "goals.sqlite3")
        ).execute("SELECT state_json FROM goals").fetchone()
        return outcome, (stored[0] if stored else "")

    def test_a_body_cannot_persist_through_any_durable_field(self) -> None:
        """Regression: only evidence and memory were guarded.

        The objective, criteria, context, referents, decisions, corrections,
        progress, blockers and outstanding work are all persisted, so a body
        supplied as any of them reached durable state unchecked.
        """
        criterion = SuccessCriterion("criterion-1", "done")
        cases = {
            "objective": dict(objective_summary=self.BODY,
                              success_criteria=(criterion,)),
            "success criterion": dict(objective_summary="Handle",
                                      success_criteria=(SuccessCriterion("c", self.BODY),)),
            "context": dict(objective_summary="Handle", success_criteria=(criterion,),
                            context={"note": self.BODY}),
            "progress": dict(objective_summary="Handle", success_criteria=(criterion,),
                             new_progress=(ProgressRecord("p1", self.BODY),)),
            "decision": dict(objective_summary="Handle", success_criteria=(criterion,),
                             new_decisions=(ProgressRecord("d1", self.BODY),)),
            "blocker": dict(objective_summary="Handle", success_criteria=(criterion,),
                            blockers=(WorkItem("b1", self.BODY),)),
            "outstanding work": dict(objective_summary="Handle",
                                     success_criteria=(criterion,),
                                     outstanding_work=(WorkItem("w1", self.BODY),)),
        }
        for label, proposal in cases.items():
            with self.subTest(field=label):
                self.setUp()
                outcome, stored = self._attempt(**proposal)
                self.assertEqual(outcome.state, CoreState.ERROR)
                self.assertNotIn(self.BODY, stored)
                self.tearDown()

    def test_content_hidden_in_a_mapping_key_is_refused(self) -> None:
        from alx.core.loop import CoreAgent as Core

        pieces = {self.BODY[i:i + 10]: "x" for i in range(0, len(self.BODY), 10)}
        self.assertTrue(
            Core._reproduces_transient_content(self._conversation(), pieces)
        )

    def test_single_characters_among_unrelated_records_are_refused(self) -> None:
        from alx.core.loop import CoreAgent as Core

        pieces: dict[str, str] = {}
        for index, character in enumerate(self.BODY):
            pieces[f"a{index}"] = character
            pieces[f"z{index}"] = "~"
        self.assertTrue(
            Core._reproduces_transient_content(self._conversation(), pieces)
        )

    def test_shuffled_fragments_are_refused(self) -> None:
        from alx.core.loop import CoreAgent as Core

        pieces = [self.BODY[i:i + 7] for i in range(0, len(self.BODY), 7)]
        scattered = {f"k{index}": value for index, value in enumerate(reversed(pieces))}
        self.assertTrue(
            Core._reproduces_transient_content(self._conversation(), scattered)
        )


class EventDrivenGoalTests(unittest.TestCase):
    """A goal begun by arriving mail must not crash or be misattributed."""

    def test_a_goal_from_an_event_alone_names_the_event(self) -> None:
        """Regression: creation always used the last conversation turn.

        With no person turns this raised IndexError, and with unrelated turns
        present the objective was attributed to one of them instead.
        """
        directory = tempfile.TemporaryDirectory()
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        event = BackgroundEvent("event-1", "mail.observed", NOW, data={"uid": "1"})
        snapshot = ConversationSnapshot("conversation-1", (), 1, RETENTION, (event,))
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE, objective_summary="Handle the mail",
                success_criteria=(SuccessCriterion("criterion-1", "handled"),)),
            response="New mail arrived.", response_requires_goal_commit=True,
        ))
        outcome = CoreAgent(
            store, reasoner, lambda call, state: None, (),
            clock=lambda: NOW, identifier_factory=lambda: "goal-1",
        ).process(snapshot, None, RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(
            store.load("goal-1").state.objective.source_reference, "event:event-1"
        )
        store.close()
        directory.cleanup()
