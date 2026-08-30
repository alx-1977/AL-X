"""Evidence for the reply primitive.

A reply is one send. The Core decides who it goes to and what it says; this
capability transmits exactly what it is handed. Nothing here inspects Friedl's
wording, and no reply workflow exists.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityResultState, MailContent, MailParticipants, MailReference,
    MailSendError, MailThreading, OutboundReply, ReplyOutcome, SideEffect,
)
from alx.providers.icloud_mail_send import ICloudMailSender  # noqa: E402
from alx.tools.mail import (  # noqa: E402
    DEFINITIONS, SEND_DEFINITIONS, SEND_MAIL_REPLY, build_send_executors,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELF = "friedl@example.test"


class FakeSender:
    address = SELF

    def __init__(self, outcome=None, error=None) -> None:
        self.sent: list[OutboundReply] = []
        self._outcome = outcome
        self._error = error

    def send_reply(self, reply: OutboundReply) -> ReplyOutcome:
        self.sent.append(reply)
        if self._error is not None:
            raise self._error
        return self._outcome or ReplyOutcome(
            "<new@example.test>", True, tuple(reply.to), ()
        )


def executor(sender):
    return build_send_executors(sender, lambda: "call-1")[SEND_MAIL_REPLY]


ARGUMENTS = {
    "to": ("jan@example.test",),
    "subject": "Re: Panel revision",
    "body": "I will be there tomorrow at 2pm.",
    "in_reply_to": "<parent@example.test>",
    "references": ("<root@example.test>", "<parent@example.test>"),
}


class ThreadingTests(unittest.TestCase):
    def test_a_reply_chain_follows_rfc_5322(self) -> None:
        threading = MailThreading(
            "<third@example.test>", "<second@example.test>",
            ("<root@example.test>", "<second@example.test>"),
        )
        # The parent's own chain, then the parent itself, order preserved.
        self.assertEqual(
            threading.reply_references(),
            ("<root@example.test>", "<second@example.test>", "<third@example.test>"),
        )

    def test_a_duplicated_identifier_is_not_repeated(self) -> None:
        threading = MailThreading("<a@example.test>", "", ("<a@example.test>",))
        self.assertEqual(threading.reply_references(), ("<a@example.test>",))

    def test_a_message_without_an_identifier_yields_an_empty_chain(self) -> None:
        self.assertEqual(MailThreading().reply_references(), ())

    def test_threading_headers_reach_the_wire_unencoded(self) -> None:
        """A MIME-encoded identifier is opaque to other clients."""
        long_id = "<" + "a" * 90 + "@example.test>"
        sender = ICloudMailSender("smtp.example.test", 587, SELF, "secret")
        reply = OutboundReply(
            to=("jan@example.test",),
            subject="Re: A subject long enough that it should still fold normally",
            body="Confirmed.",
            in_reply_to=long_id,
            references=(long_id,),
        )
        rendered = sender.build_message(reply, long_id).as_string()
        self.assertNotIn("=?utf-8?", rendered)
        self.assertIn(long_id, rendered)

    def test_the_sender_identity_is_configuration_and_cannot_be_chosen(self) -> None:
        self.assertNotIn("sender", OutboundReply.__dataclass_fields__)
        self.assertNotIn("from_address", OutboundReply.__dataclass_fields__)
        definition = SEND_DEFINITIONS[0]
        for field_name in ("from", "sender", "from_address", "reply_from"):
            self.assertNotIn(field_name, definition.input_schema.properties)
        sender = ICloudMailSender("smtp.example.test", 587, SELF, "secret")
        rendered = sender.build_message(
            OutboundReply(to=("jan@example.test",), subject="s", body="b"),
            "<x@example.test>",
        ).as_string()
        self.assertIn(f"From: {SELF}", rendered)


class ReadExposesReplyDataTests(unittest.TestCase):
    def test_read_reports_participants_and_identifiers(self) -> None:
        content = MailContent(
            MailReference("INBOX", "1", "2"), "Panel", "Jan <jan@example.test>",
            "Fri, 28 Aug 2026 09:15:00 +0200", "body",
            MailParticipants(
                sender="Jan <jan@example.test>",
                reply_to="jan.direct@example.test",
                recipients=(SELF,),
                carbon_copy=("records@example.test",),
            ),
            MailThreading("<p@example.test>", "", ()),
        )
        self.assertEqual(content.participants.reply_to, "jan.direct@example.test")
        self.assertEqual(content.threading.reply_references(), ("<p@example.test>",))

    def test_the_read_capability_declares_the_reply_fields(self) -> None:
        read = next(d for d in DEFINITIONS if d.capability_id == "read_mail_message")
        for field_name in ("reply_to", "recipients", "carbon_copy",
                           "message_id", "reply_references"):
            self.assertIn(field_name, read.output_schema.properties)

    def test_identifiers_are_durable_but_the_body_is_not(self) -> None:
        """Addresses and identifiers are references, not message content."""
        source = (REPOSITORY_ROOT / "src/alx/tools/mail.py").read_text("utf-8")
        durable_block = source.split("durable: dict[str, Any] = {")[1].split("}")[0]
        for field_name in ("message_id", "reply_references", "reply_to"):
            self.assertIn(field_name, durable_block)
        self.assertNotIn('"body"', durable_block)


class SendExecutorTests(unittest.TestCase):
    def test_a_reply_transmits_exactly_what_it_was_handed(self) -> None:
        sender = FakeSender()
        result = executor(sender)(ARGUMENTS)
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        sent = sender.sent[0]
        self.assertEqual(sent.to, ("jan@example.test",))
        self.assertEqual(sent.body, "I will be there tomorrow at 2pm.")
        self.assertEqual(sent.in_reply_to, "<parent@example.test>")
        self.assertEqual(result.values["sender_address"], SELF)

    def test_an_incomplete_address_is_refused_before_sending(self) -> None:
        sender = FakeSender()
        result = executor(sender)({**ARGUMENTS, "to": ("not-an-address",)})
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(sender.sent, [])

    def test_a_reply_with_no_recipient_is_refused(self) -> None:
        sender = FakeSender()
        result = executor(sender)({**ARGUMENTS, "to": ()})
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(sender.sent, [])

    def test_an_empty_body_is_refused(self) -> None:
        sender = FakeSender()
        result = executor(sender)({**ARGUMENTS, "body": ""})
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(sender.sent, [])

    def test_a_refused_recipient_is_reported_as_partial(self) -> None:
        outcome = ReplyOutcome("<new@example.test>", True, (), ("bounced@example.test",))
        result = executor(FakeSender(outcome=outcome))(ARGUMENTS)
        self.assertIs(result.state, CapabilityResultState.PARTIAL)
        self.assertEqual(result.values["recipients_refused"], ("bounced@example.test",))

    def test_an_ambiguous_outcome_is_reported_not_retried(self) -> None:
        sender = FakeSender(error=MailSendError("send_outcome_unknown"))
        result = executor(sender)(ARGUMENTS)
        self.assertEqual(result.failure["code"], "send_outcome_unknown")
        self.assertEqual(len(sender.sent), 1, "an ambiguous send must not repeat")

    def test_a_failure_never_leaks_the_body_or_a_credential(self) -> None:
        sender = FakeSender(error=MailSendError("authentication_failed"))
        result = executor(sender)(ARGUMENTS)
        rendered = str(result.failure)
        self.assertNotIn("I will be there tomorrow", rendered)
        self.assertNotIn("secret", rendered)

    def test_the_result_satisfies_the_declared_schema(self) -> None:
        result = executor(FakeSender())(ARGUMENTS)
        self.assertTrue(SEND_DEFINITIONS[0].output_schema.accepts(result.values))

    def test_the_transport_never_retries_internally(self) -> None:
        tree = ast.parse(
            (REPOSITORY_ROOT / "src/alx/providers/icloud_mail_send.py").read_text("utf-8")
        )
        send = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "send_reply")
        self.assertEqual(
            [n for n in ast.walk(send) if isinstance(n, (ast.For, ast.While))
             and not isinstance(getattr(n, "target", None), ast.Name)],
            [], "transmission must not loop",
        )


class NoReplyWorkflowTests(unittest.TestCase):
    """Law 1, 5 and 6: one primitive, no journey, no phrase routing."""

    SOURCES = (
        "src/alx/tools/mail.py",
        "src/alx/providers/icloud_mail_send.py",
        "src/alx/contracts/mail.py",
    )

    def test_exactly_one_send_capability_exists(self) -> None:
        self.assertEqual([d.capability_id for d in SEND_DEFINITIONS], [SEND_MAIL_REPLY])
        self.assertIs(SEND_DEFINITIONS[0].side_effect, SideEffect.EFFECTFUL)

    def test_no_composed_reply_journey_was_added(self) -> None:
        identifiers = {d.capability_id for d in (*DEFINITIONS, *SEND_DEFINITIONS)}
        for forbidden in ("reply_to_email", "compose_and_send_reply", "draft_reply",
                          "read_and_reply", "handle_email", "process_email"):
            self.assertNotIn(forbidden, identifiers)

    def test_the_capability_composes_no_wording(self) -> None:
        """It transmits what it is handed; it never writes a subject or body."""
        source = (REPOSITORY_ROOT / "src/alx/tools/mail.py").read_text("utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "_outbound_reply")
        rendered = ast.unparse(function)
        # No default text, prefix, or template is supplied anywhere.
        for scripted in ("Re:", "RE:", "Fwd:", "Dear ", "Kind regards", "Sent from"):
            self.assertNotIn(scripted, rendered)

    def test_no_added_source_declares_a_workflow_or_router(self) -> None:
        forbidden = {"workflow", "workflows", "intent", "intents", "route", "routes",
                     "router", "handler", "trigger", "phrase", "keyword", "sequence"}
        for relative_path in self.SOURCES:
            tree = ast.parse((REPOSITORY_ROOT / relative_path).read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.assertFalse(set(node.name.lower().split("_")) & forbidden,
                                     f"{relative_path}: {node.name}")

    def test_no_send_source_reaches_a_conversational_boundary(self) -> None:
        for relative_path in self.SOURCES:
            tree = ast.parse((REPOSITORY_ROOT / relative_path).read_text("utf-8"))
            imported = {n.module for n in ast.walk(tree)
                        if isinstance(n, ast.ImportFrom) and n.module}
            for forbidden in ("alx.core", "alx.conversation", "alx.goals", "alx.memories"):
                self.assertNotIn(forbidden, imported)

    def test_no_input_field_accepts_raw_user_language(self) -> None:
        language_fields = {"text", "message", "prompt", "query", "utterance",
                           "transcript", "user_input", "request", "instruction",
                           "phrase", "intent", "command"}
        self.assertFalse(
            set(SEND_DEFINITIONS[0].input_schema.properties) & language_fields
        )

    def test_sending_is_separate_from_observation(self) -> None:
        """Reading must never imply sending."""
        observation = {d.capability_id for d in DEFINITIONS}
        self.assertNotIn(SEND_MAIL_REPLY, observation)

    def test_sending_carries_its_own_authorisation_and_permission(self) -> None:
        """Law 19: production action needs a recorded authorisation."""
        from alx.bootstrap.mail import (
            MAIL_READ_PERMISSION, MAIL_SEND_PERMISSION, MAIL_TRASH_PERMISSION,
        )

        self.assertNotIn(MAIL_SEND_PERMISSION,
                         {MAIL_READ_PERMISSION, MAIL_TRASH_PERMISSION})
        decisions = (REPOSITORY_ROOT / "governance/DECISIONS.md").read_text("utf-8")
        self.assertIn("D-011", decisions)
        self.assertIn(SEND_MAIL_REPLY, decisions)

    def test_every_send_requires_its_own_expiring_exact_approval(self) -> None:
        from alx.bootstrap.mail import build_mail_send_runtime
        from alx.config import MailSendSettings

        settings = MailSendSettings.from_environment({
            "MAIL_ADDRESS": SELF, "MAIL_KEY": "secret",
            "MAIL_SMTP_HOST": "smtp.example.test", "MAIL_SMTP_PORT": "587",
        })
        _definitions, policies, _executors, permissions = build_mail_send_runtime(
            settings, lambda: "call-1"
        )
        self.assertTrue(policies[SEND_MAIL_REPLY].approval_required)
        self.assertEqual(sorted(permissions), ["mail.send"])
        # Ten minutes, as recorded in D-011.
        self.assertEqual(settings.approval_ttl_seconds, 600)

    def test_the_gates_pass_over_the_added_sources(self) -> None:
        from scripts.check_architecture import check_source, load_rules

        self.assertEqual(
            [i.render() for i in check_source(REPOSITORY_ROOT, load_rules(REPOSITORY_ROOT))],
            [],
        )


if __name__ == "__main__":
    unittest.main()


class AskingIsFreeTests(unittest.TestCase):
    """AL/X never needs permission, or a goal status, to ask a question."""

    def test_no_impossible_goal_mutation_is_offered_to_the_model(self) -> None:
        """Regression: await_approval always failed, so choosing it was a trap.

        It demanded a requested approval record that nothing in the Core
        creates. AL/X selected it, was refused, retried, and the session ended.
        """
        from alx.contracts import GoalMutationKind
        from alx.core.model_reasoner import decision_schema

        schema = decision_schema()
        rendered = str(schema)
        self.assertNotIn(GoalMutationKind.AWAIT_APPROVAL.value, rendered)
        # Every other mutation remains available; nothing else was narrowed.
        for kind in GoalMutationKind:
            if kind is not GoalMutationKind.AWAIT_APPROVAL:
                self.assertIn(kind.value, rendered)

    def test_the_model_is_told_that_asking_needs_no_permission(self) -> None:
        source = (REPOSITORY_ROOT / "src/alx/core/model_reasoner.py").read_text("utf-8")
        self.assertIn("never need permission to ask a question", source)

    def test_asking_requires_no_scripted_sequence(self) -> None:
        """Law 1 and 6: no step may be required before AL/X may speak."""
        source = (REPOSITORY_ROOT / "src/alx/core/model_reasoner.py").read_text("utf-8")
        for scripted in ("must first", "before asking", "only after",
                         "step 1", "then ask"):
            self.assertNotIn(scripted, source.lower())


class UnheardTextTests(unittest.TestCase):
    """AL/X cannot send outward wording Friedl has never heard from her.

    This is a property of the artifact, not an ordering rule. She may draft,
    ask, argue, or abandon a message in any order and needs no permission to
    speak. She simply cannot transmit words he never heard, which is what stops
    her reporting one message and sending another.
    """

    @staticmethod
    def _conversation(*turns):
        from datetime import UTC, datetime, timedelta
        from alx.contracts import (
            ConversationOrigin, ConversationSnapshot, ConversationTurn,
        )

        now = datetime(2026, 8, 30, tzinfo=UTC)
        built = tuple(
            ConversationTurn(
                "conversation-1", f"turn-{index + 1}",
                ConversationOrigin.ALX_RESPONSE if spoken_by_alx
                else ConversationOrigin.TYPED,
                text, now, None if spoken_by_alx else "friedl",
            )
            for index, (spoken_by_alx, text) in enumerate(turns)
        )
        return ConversationSnapshot(
            "conversation-1", built, len(built), now + timedelta(days=1)
        )

    @staticmethod
    def _call(body: str):
        from alx.contracts import CapabilityCall

        return CapabilityCall(
            "call-1", SEND_MAIL_REPLY,
            {"to": ("john@example.test",), "subject": "Re: Part", "body": body},
            "approval-1",
        )

    def test_wording_he_never_heard_is_refused(self) -> None:
        """The exact failure observed live: sent first, quoted afterwards."""
        from alx.core.loop import CoreAgent

        conversation = self._conversation(
            (False, "You can let John know the part will be here tomorrow."),
        )
        self.assertTrue(
            CoreAgent._unheard_authored_text(
                conversation, self._call("Hi John, the part arrives tomorrow.")
            )
        )

    def test_wording_he_has_heard_is_permitted(self) -> None:
        from alx.core.loop import CoreAgent

        drafted = "Hi John, I have checked and the part should arrive tomorrow."
        conversation = self._conversation(
            (False, "Let John know the part will be here tomorrow."),
            (True, f"I will send: {drafted} Shall I?"),
            (False, "Yes, send it."),
        )
        self.assertFalse(
            CoreAgent._unheard_authored_text(conversation, self._call(drafted))
        )

    def test_reformatted_whitespace_still_counts_as_heard(self) -> None:
        from alx.core.loop import CoreAgent

        conversation = self._conversation(
            (True, "I will send: Hi John,  the part\narrives tomorrow."),
        )
        self.assertFalse(
            CoreAgent._unheard_authored_text(
                conversation, self._call("Hi John, the part arrives tomorrow.")
            )
        )

    def test_his_own_words_do_not_count_as_her_having_said_it(self) -> None:
        """Only what AL/X said counts; echoing his instruction is not enough."""
        from alx.core.loop import CoreAgent

        body = "The part will be here tomorrow."
        conversation = self._conversation((False, body))
        self.assertTrue(
            CoreAgent._unheard_authored_text(conversation, self._call(body))
        )

    def test_a_capability_carrying_no_authored_text_is_unaffected(self) -> None:
        """Reading, acknowledging and trashing are untouched by this rule."""
        from alx.contracts import CapabilityCall
        from alx.core.loop import CoreAgent

        conversation = self._conversation((False, "Trash that one."))
        for capability_id in ("read_mail_message", "move_mail_message_to_trash"):
            call = CapabilityCall(
                "call-1", capability_id,
                {"mailbox_id": "INBOX", "uid_validity": "1", "uid": "2"},
            )
            self.assertFalse(CoreAgent._unheard_authored_text(conversation, call))

    def test_the_core_refuses_a_send_carrying_unheard_wording(self) -> None:
        """Exercises the enforcement path, not just the helper."""
        import tempfile
        from datetime import UTC, datetime, timedelta
        from alx.contracts import (
            AgentDecision, ApprovalProposal, ApprovalScope, CapabilityCall,
            CapabilityDefinition, ConversationOrigin, ConversationSnapshot,
            ConversationTurn, GoalMutationKind, GoalProposal, StructuredSchema,
            SuccessCriterion, ValueKind,
        )
        from alx.core import CoreAgent, CoreState
        from alx.goals import SQLiteGoalStore

        now = datetime(2026, 8, 30, tzinfo=UTC)
        schema = StructuredSchema(ValueKind.OBJECT)
        definition = CapabilityDefinition(
            SEND_MAIL_REPLY, "reply", schema, schema, SideEffect.EFFECTFUL
        )
        arguments = {"to": ("john@example.test",), "subject": "Re: Part",
                     "body": "Hi John, the part arrives tomorrow."}
        turns = (
            ConversationTurn("c", "t1", ConversationOrigin.TYPED,
                             "Let John know the part arrives tomorrow.", now, "friedl"),
        )
        conversation = ConversationSnapshot("c", turns, 1, now + timedelta(days=1))
        dispatched: list[CapabilityCall] = []

        def dispatch(proposed, state):
            dispatched.append(proposed)
            raise AssertionError("unheard wording must never be dispatched")

        reasoner = Queued(AgentDecision(
            goal_proposal=GoalProposal(
                kind=GoalMutationKind.CREATE, objective_summary="Reply",
                success_criteria=(SuccessCriterion("criterion-1", "sent"),)),
            call=CapabilityCall("call-1", SEND_MAIL_REPLY, arguments, "approval-1"),
            approval_proposal=ApprovalProposal(
                "approval-1", ApprovalScope(SEND_MAIL_REPLY, arguments), "turn:t1"),
        ))
        directory = tempfile.TemporaryDirectory()
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        outcome = CoreAgent(
            store, reasoner, dispatch, (definition,),
            clock=lambda: now, identifier_factory=lambda: "goal-1",
        ).process(conversation, None, now + timedelta(days=1), 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "approval_proposal_invalid")
        self.assertEqual(dispatched, [], "nothing may be transmitted")
        store.close()
        directory.cleanup()

    def test_the_rule_imposes_no_ordering_or_state(self) -> None:
        """Law 1 and 6: nothing requires a step before AL/X may speak or act."""
        source = (REPOSITORY_ROOT / "src/alx/core/loop.py").read_text("utf-8")
        for scripted in ("must first", "step_one", "awaiting_read_back",
                         "pending_confirmation", "read_back_state"):
            self.assertNotIn(scripted, source.lower())


class ApprovalExpiryTests(unittest.TestCase):
    """D-011 promises a ten minute window; it must actually be applied."""

    def test_a_granted_approval_carries_the_configured_expiry(self) -> None:
        import tempfile
        from datetime import UTC, datetime, timedelta
        from alx.contracts import (
            AgentDecision, ApprovalProposal, ApprovalScope, CapabilityCall,
            CapabilityDefinition, ConversationOrigin, ConversationSnapshot,
            ConversationTurn, GoalMutationKind, GoalProposal, StructuredSchema,
            SuccessCriterion, ValueKind,
        )
        from alx.core import CoreAgent
        from alx.goals import SQLiteGoalStore

        now = datetime(2026, 8, 30, tzinfo=UTC)
        schema = StructuredSchema(ValueKind.OBJECT)
        definition = CapabilityDefinition(
            "act", "one action", schema, schema, SideEffect.EFFECTFUL
        )
        arguments = {"value": "heard text"}
        call = CapabilityCall("call-1", "act", arguments, "approval-1")
        turns = (
            ConversationTurn("c", "t1", ConversationOrigin.TYPED, "do it", now, "friedl"),
        )
        conversation = ConversationSnapshot("c", turns, 1, now + timedelta(days=1))
        reasoner = Queued(
            AgentDecision(
                goal_proposal=GoalProposal(
                    kind=GoalMutationKind.CREATE, objective_summary="Act",
                    success_criteria=(SuccessCriterion("criterion-1", "done"),)),
                call=call,
                approval_proposal=ApprovalProposal(
                    "approval-1", ApprovalScope("act", arguments), "turn:t1"),
            ),
            AgentDecision(response="Done."),
        )
        directory = tempfile.TemporaryDirectory()
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        from alx.contracts import (
            CapabilityAttempt, CapabilityAttemptDisposition, CapabilityResult,
            CapabilityResultState,
        )

        def dispatch(proposed, state):
            return CapabilityAttempt(
                proposed, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(proposed.call_id, proposed.capability_id,
                                 CapabilityResultState.SUCCEEDED, {}),
            )

        agent = CoreAgent(
            store, reasoner, dispatch, (definition,),
            clock=lambda: now, identifier_factory=lambda: "goal-1",
            approval_ttl_seconds=600,
        )
        agent.process(conversation, None, now + timedelta(days=1), 2)
        approval = store.load("goal-1").state.approvals[0]
        self.assertEqual(approval.expires_at, now + timedelta(minutes=10))
        store.close()
        directory.cleanup()

    def test_a_non_positive_window_is_refused(self) -> None:
        import tempfile
        from alx.core import CoreAgent
        from alx.goals import SQLiteGoalStore

        directory = tempfile.TemporaryDirectory()
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        with self.assertRaises(ValueError):
            CoreAgent(store, None, None, (), approval_ttl_seconds=0)
        store.close()
        directory.cleanup()


class Queued:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)
