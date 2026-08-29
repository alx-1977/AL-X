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

    def test_send_is_not_registered_without_a_governance_decision(self) -> None:
        """Law 19: production mutation needs a recorded authorisation."""
        registered = {d.capability_id for d in DEFINITIONS}
        self.assertNotIn(SEND_MAIL_REPLY, registered)

    def test_the_gates_pass_over_the_added_sources(self) -> None:
        from scripts.check_architecture import check_source, load_rules

        self.assertEqual(
            [i.render() for i in check_source(REPOSITORY_ROOT, load_rules(REPOSITORY_ROOT))],
            [],
        )


if __name__ == "__main__":
    unittest.main()
