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
SEEN = "mark_mail_message_seen"
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
            MAIL_READ_PERMISSION, MAIL_SEEN_PERMISSION, MAIL_TRASH_PERMISSION,
        )

        self.assertNotEqual(MAIL_TRASH_PERMISSION, MAIL_READ_PERMISSION)
        self.assertEqual(
            len({MAIL_READ_PERMISSION, MAIL_SEEN_PERMISSION, MAIL_TRASH_PERMISSION}),
            3,
        )

    def test_only_authorised_mail_mutations_carry_external_side_effects(self) -> None:
        from alx.tools import DEFINITIONS

        effectful = [item.capability_id for item in DEFINITIONS
                     if item.side_effect is SideEffect.EFFECTFUL]
        self.assertEqual(effectful, [SEEN, TRASH])

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
        self.assertIn("D-015", decisions)
        self.assertIn(SEEN, decisions)


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

    def test_a_step_budget_checkpoint_keeps_the_conversation_open(self) -> None:
        """Reaching the step budget is durable progress, not a failure.

        The Core persists the goal and can continue it, so a long multi-step
        task must not hang up the voice transport mid-goal.
        """
        from alx.interfaces.server import RECOVERABLE_TRANSPORT_REASONS

        self.assertIn("budget_exhausted", RECOVERABLE_TRANSPORT_REASONS)

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


class EventDrivenGoalTests(unittest.TestCase):
    """A goal begun by arriving mail must not crash or be misattributed."""

    def test_an_event_goal_in_an_established_conversation_names_the_event(self) -> None:
        """Regression: the latest person turn was preferred unconditionally.

        A message arriving during an ongoing conversation had its goal
        attributed to whatever was last said, which is unrelated to the message
        that triggered it. The previous test only covered an empty
        conversation, so it proved nothing about this case.
        """
        directory = tempfile.TemporaryDirectory()
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        event = BackgroundEvent("event-1", "mail.observed", NOW, data={"uid": "1"})
        earlier = ConversationTurn(
            "conversation-1", "old-turn", ConversationOrigin.TYPED,
            "unrelated conversation from earlier", NOW, "friedl",
        )
        snapshot = ConversationSnapshot(
            "conversation-1", (earlier,), 1, RETENTION, (event,)
        )
        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE, objective_summary="Handle the mail",
                success_criteria=(SuccessCriterion("criterion-1", "handled"),)),
            response="New mail arrived.", response_requires_goal_commit=True,
        ))
        outcome = CoreAgent(
            store, reasoner, lambda call, state: None, (),
            clock=lambda: NOW, identifier_factory=lambda: "goal-1",
        ).process(snapshot, None, RETENTION, 2, trigger_event_id="event-1")
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(
            store.load("goal-1").state.objective.source_reference, "event:event-1"
        )
        store.close()
        directory.cleanup()

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


class TransientRetentionGapTests(unittest.TestCase):
    """The similarity guard is gone; provenance is wired but not enforced.

    Six versions were attempted. Each either leaked when the content was
    fragmented or refused ordinary summaries; the last committed version did
    the latter, so it was actively preventing AL/X from describing a message.

    Provenance-based retention in docs/MAIL_RETENTION_PROPOSAL.md is the agreed
    replacement, approved as D-013 and wired into new writes. These tests record
    the remaining enforcement gap honestly so its closure is deliberate rather
    than assumed.
    """

    def test_no_similarity_guard_remains_in_the_core(self) -> None:
        source = (Path(__file__).resolve().parents[1]
                  / "src/alx/core/loop.py").read_text("utf-8")
        for removed in ("_reproduces_transient_content", "_assembles_from",
                        "_TRANSIENT_QUOTE_CHARACTERS", "_proposal_reproduces"):
            self.assertNotIn(removed, source)

    def test_no_content_blocker_is_misrepresented_as_expiry_enforcement(self) -> None:
        """Recorded, not asserted as acceptable.

        A model-derived copy receives a deadline, but remains reachable because
        no scheduled purge enforces it. A removed similarity guard is not a
        substitute for that missing enforcement.
        """
        source = (Path(__file__).resolve().parents[1]
                  / "src/alx/core/loop.py").read_text("utf-8")
        self.assertNotIn("goal_reproduces_transient_content", source)

    def test_the_replacement_design_records_the_unenforced_expiry_gap(self) -> None:
        proposal = (Path(__file__).resolve().parents[1]
                    / "docs/MAIL_RETENTION_PROPOSAL.md").read_text("utf-8")
        self.assertIn("Policy approved as D-013", proposal)
        self.assertIn("Provenance now flows mechanically", proposal)
        self.assertIn("No scheduled purge enforces those deadlines yet", proposal)
        self.assertIn("provenance", proposal.lower())
        # Raw bodies are still never automatically persisted by the provider.
        mail_tools = (Path(__file__).resolve().parents[1]
                      / "src/alx/tools/mail.py").read_text("utf-8")
        self.assertIn("durable_values=durable", mail_tools)
