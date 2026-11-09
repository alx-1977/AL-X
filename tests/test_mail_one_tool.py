"""Mail as one capability with an action per job (D-074, amended 2026-11-09).

Nine mail capabilities became `mail`: search, read, list_attachments,
read_attachment, acknowledge, mark_seen, flag, file and trash. Sending stays
`send_mail_reply` (it needs approval) and the document reader stays
`read_mail_document` (optional, paid, its own permission). Each action keeps
its own contract; results pass through unwrapped so mail content keeps its
D-013 provenance.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.mail import (  # noqa: E402
    MAIL_OBSERVATION_PERMISSION, MAIL_READ_PERMISSION, MAIL_SEEN_PERMISSION,
    MAIL_TRASH_PERMISSION, build_mail_runtime, mail_authority_policies,
    mail_post_reply_standing_scopes,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.config import MailSettings  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityResult,
    CapabilityResultState, ConversationSnapshot, GoalState, MailReference, Objective,
    SideEffect, SuccessCriterion,
)
from alx.core.loop import CoreAgent  # noqa: E402
from alx.core.model_reasoner import _one_line  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools import mail as mail_tools  # noqa: E402
from alx.tools.mail import (  # noqa: E402
    CONTRACTS, DEFINITION, DEFINITIONS, MAIL, SEND_DEFINITIONS, MailAction,
    build_mail_executors,
)
from alx.tools.worker import WORKER_CAPABILITIES, WORKER_LIMITS, worker_call_refusal  # noqa: E402
from mail_actions import (  # noqa: E402
    ACKNOWLEDGE, FILE, FLAG, LIST_ATTACHMENTS, MARK_SEEN, READ, READ_ATTACHMENT, SEARCH,
    TRASH, mail_arguments, mail_call, mail_scope,
)
from test_mail_vertical_slice import FakeAccount  # noqa: E402

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
REFERENCE = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "2"}
OLD_IDS = (
    "search_mail_messages", "read_mail_message", "list_mail_attachments",
    "read_mail_attachment", "acknowledge_mail_message", "mark_mail_message_seen",
    "set_mail_flag_colour", "file_processed_mail_message", "move_mail_message_to_trash",
)


def runtime(directory: str):
    return build_mail_runtime(
        MailSettings("friedl@example.test", "secret", "imap.example.test", 993, 15, "Processed"),
        Path(directory), lambda: "call-1",
    )


class OneCapabilityTests(unittest.TestCase):
    def test_the_runtime_registers_one_mail_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            built = runtime(directory)
            try:
                self.assertEqual([item.capability_id for item in built.definitions], [MAIL])
                self.assertEqual(set(built.executors), {MAIL})
                self.assertEqual(set(built.policies), {MAIL})
            finally:
                built.observations.close()
        self.assertEqual(DEFINITIONS, (DEFINITION,))
        # Sending stays separate and needs approval.
        self.assertEqual([item.capability_id for item in SEND_DEFINITIONS], ["send_mail_reply"])

    def test_no_superseded_mail_id_remains_in_production_code(self) -> None:
        """Law 0: the nine ids are gone, not hidden, wrapped or renamed. They are
        only recognised in goals saved before D-074, in one place, and none can
        be called."""
        recognised = ROOT / "src/alx/contracts/mail.py"
        for path in (ROOT / "src/alx").rglob("*.py"):
            if path == recognised:
                continue
            source = path.read_text(encoding="utf-8")
            for old in OLD_IDS:
                with self.subTest(path=path.name, old=old):
                    self.assertNotIn(old, source)
        from alx.contracts.mail import _RECORDED_BEFORE_D074
        self.assertEqual(set(_RECORDED_BEFORE_D074), set(OLD_IDS))
        self.assertFalse(set(OLD_IDS) & {item.capability_id for item in DEFINITIONS})
        for name in dir(mail_tools):
            self.assertFalse(name.endswith(("_MAIL_MESSAGE", "_MAIL_MESSAGES", "_MAIL_ATTACHMENT",
                                            "_MAIL_ATTACHMENTS", "_MAIL_FLAG_COLOUR")), name)

    def test_the_catalogue_line_names_every_job(self) -> None:
        line = _one_line(DEFINITION.purpose)
        self.assertEqual(
            line,
            "Everything with Friedl's mail except sending: search, read messages and "
            "attachments, acknowledge, mark read, flag, file and move to Trash.",
        )

    def test_each_action_keeps_its_own_contract_and_lists_its_fields(self) -> None:
        self.assertEqual(set(CONTRACTS), set(MailAction))
        self.assertEqual(DEFINITION.input_schema.required, ("action",))
        self.assertFalse(DEFINITION.input_schema.extra_properties)
        for action, contract in CONTRACTS.items():
            with self.subTest(action=action):
                self.assertIn(f"[{action.value}] fields: ", DEFINITION.purpose)
                self.assertIn(contract.purpose, DEFINITION.purpose)
                self.assertTrue(set(contract.input_schema.properties)
                                <= set(DEFINITION.input_schema.properties))
                self.assertTrue(set(contract.output_schema.properties)
                                <= set(DEFINITION.output_schema.properties))

    def test_one_policy_with_every_permission_and_no_approval(self) -> None:
        (policy,) = mail_authority_policies().values()
        self.assertEqual(policy.permission_references, frozenset({
            MAIL_READ_PERMISSION, MAIL_OBSERVATION_PERMISSION,
            MAIL_SEEN_PERMISSION, MAIL_TRASH_PERMISSION,
        }))
        self.assertFalse(policy.approval_required)


class EffectByActionTests(unittest.TestCase):
    def test_reads_change_nothing_acknowledge_is_attention_and_the_rest_change_mail(self) -> None:
        expected = {
            SEARCH: SideEffect.NONE, READ: SideEffect.NONE,
            LIST_ATTACHMENTS: SideEffect.NONE, READ_ATTACHMENT: SideEffect.NONE,
            ACKNOWLEDGE: SideEffect.ATTENTION_STATE,
            MARK_SEEN: SideEffect.EFFECTFUL, FLAG: SideEffect.EFFECTFUL,
            FILE: SideEffect.EFFECTFUL, TRASH: SideEffect.EFFECTFUL,
        }
        self.assertEqual(set(expected), {item.value for item in MailAction})
        for action, effect in expected.items():
            with self.subTest(action=action):
                self.assertIs(DEFINITION.effect_for(mail_arguments(action, REFERENCE)), effect)
        # An unknown action is treated as the capability's own class.
        self.assertIs(DEFINITION.effect_for({"action": "delete"}), SideEffect.EFFECTFUL)

    def test_reads_and_acknowledge_need_no_active_goal_but_changes_do(self) -> None:
        core = CoreAgent.__new__(CoreAgent)
        core._capabilities = (DEFINITION,)
        core._has_pending_dispatch = lambda state: False
        core._definition = lambda capability_id: DEFINITION
        for action in (SEARCH, READ, LIST_ATTACHMENTS, READ_ATTACHMENT, ACKNOWLEDGE):
            with self.subTest(action=action):
                self.assertIsNone(core._dispatch_blocked_reason(mail_call("c", action, REFERENCE), None))
        for action in (MARK_SEEN, FLAG, FILE, TRASH):
            with self.subTest(action=action):
                self.assertEqual(core._dispatch_blocked_reason(mail_call("c", action, REFERENCE), None),
                                 "active_goal_required")


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = FakeAccount()
        self.call_id = ["call-1"]
        self.broker = CapabilityBroker(
            CapabilityRegistry(DEFINITIONS),
            SafetyGate(mail_authority_policies()),
            build_mail_executors(self.account, self.account, lambda: self.call_id[0]),
        )
        self.authority = AuthorityContext("friedl", frozenset({
            MAIL_READ_PERMISSION, MAIL_OBSERVATION_PERMISSION,
            MAIL_SEEN_PERMISSION, MAIL_TRASH_PERMISSION,
        }), NOW)

    def dispatch(self, call_id, action, arguments):
        self.call_id[0] = call_id
        return self.broker.dispatch(mail_call(call_id, action, arguments), self.authority)

    def test_a_read_keeps_its_values_durable_values_and_provenance(self) -> None:
        attempt = self.dispatch("read-1", READ, REFERENCE)
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        result = attempt.result
        self.assertEqual(result.capability_id, MAIL)
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        # Unwrapped: the read's own fields at the top, not under a wrapper.
        self.assertEqual(result.values["subject"], "Quote")
        self.assertEqual(result.values["body"], "private body")
        self.assertNotIn("body", result.durable_values)
        self.assertEqual(result.recovery_values()["subject"], "Quote")
        self.assertEqual(result.provenance.mail_references, (MailReference("INBOX", "777", "2"),))
        self.assertEqual(result.provenance.content_expires_at,
                         result.provenance.recorded_at + timedelta(days=30))

    def test_changes_reach_the_account(self) -> None:
        self.assertTrue(self.dispatch("ack-1", ACKNOWLEDGE, REFERENCE).result.values["acknowledged"])
        self.assertTrue(self.dispatch("seen-1", MARK_SEEN, REFERENCE).result.values["seen"])
        moved = self.dispatch("trash-1", TRASH, REFERENCE).result
        self.assertTrue(moved.values["moved"])
        self.assertEqual(moved.values["destination_reference"]["mailbox_id"], "Deleted Messages")
        self.assertEqual(len(self.account.acknowledged), 1)
        self.assertEqual(len(self.account.seen), 1)
        self.assertEqual(len(self.account.trashed), 1)

    def test_an_unknown_action_names_the_actions(self) -> None:
        result = self.dispatch("x-1", "delete", REFERENCE).result
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "arguments_unusable")
        for action in MailAction:
            self.assertIn(action.value, result.failure["reason"])
        self.assertEqual(self.account.trashed, [])

    def test_each_action_is_held_to_its_own_fields(self) -> None:
        # A field another action takes is refused, and a missing one too.
        for action, arguments in (
            (READ, {**REFERENCE, "colour": "orange"}),
            (FLAG, REFERENCE),
            (SEARCH, {"sender": "dhl"}),
            (TRASH, {"mailbox_id": "INBOX"}),
        ):
            with self.subTest(action=action):
                result = self.dispatch("x-1", action, arguments).result
                self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(self.account.trashed, [])


class PostReplyScopeTests(unittest.TestCase):
    def state(self, *extra):
        call = CapabilityCall("send-1", "send_mail_reply", {**REFERENCE, "to": ["a@b"],
                                                            "subject": "Re", "body": "x"})
        sent = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult(
            "send-1", "send_mail_reply", CapabilityResultState.SUCCEEDED,
            {"accepted": True, "source_has_attachments": False}))
        return GoalState("goal-1", Objective("turn:1", "Reply"),
                         (SuccessCriterion("c1", "replied"),), attempts=(sent, *extra))

    def test_scopes_name_the_mail_action_and_the_exact_message(self) -> None:
        self.assertEqual(list(mail_post_reply_standing_scopes(self.state())),
                         [mail_scope(MARK_SEEN, REFERENCE), mail_scope(TRASH, REFERENCE)])
        self.assertTrue(mail_scope(TRASH, REFERENCE).matches(mail_call("t", TRASH, REFERENCE)))
        self.assertFalse(mail_scope(TRASH, REFERENCE).matches(mail_call("t", FILE, REFERENCE)))

    def test_a_trash_already_invoked_is_not_offered_again(self) -> None:
        trashed = CapabilityAttempt(mail_call("trash-1", TRASH, REFERENCE),
                                    CapabilityAttemptDisposition.EXECUTED, True,
                                    CapabilityResult("trash-1", MAIL, CapabilityResultState.FAILED,
                                                     failure={"code": "move_failed"}))
        self.assertEqual(list(mail_post_reply_standing_scopes(self.state(trashed))),
                         [mail_scope(MARK_SEEN, REFERENCE)])
        # Another mail action on the same message is not a trash.
        filed = CapabilityAttempt(mail_call("file-1", FILE, REFERENCE),
                                  CapabilityAttemptDisposition.EXECUTED, True,
                                  CapabilityResult("file-1", MAIL, CapabilityResultState.FAILED,
                                                   failure={"code": "move_failed"}))
        self.assertEqual(len(mail_post_reply_standing_scopes(self.state(filed))), 2)


class WorkerLimitTests(unittest.TestCase):
    def test_the_worker_may_only_read_an_attachment(self) -> None:
        self.assertIn(MAIL, WORKER_CAPABILITIES)
        self.assertEqual(dict(WORKER_LIMITS)[MAIL], ((("action", READ_ATTACHMENT),),))
        allowed = mail_call("w-1", READ_ATTACHMENT, {**REFERENCE, "attachment_id": "1"})
        self.assertIsNone(worker_call_refusal(allowed))
        for action in (SEARCH, READ, LIST_ATTACHMENTS, ACKNOWLEDGE, MARK_SEEN, FLAG, FILE, TRASH):
            with self.subTest(action=action):
                refused = worker_call_refusal(mail_call("w-2", action, REFERENCE))
                self.assertIs(refused.disposition, CapabilityAttemptDisposition.REJECTED)
                self.assertEqual(refused.reason_code, "worker_action_not_allowed")
                self.assertFalse(refused.implementation_invoked)
        self.assertIsNotNone(worker_call_refusal(CapabilityCall("w-3", MAIL, {})))
        # A capability without a limit is left to the gate.
        self.assertIsNone(worker_call_refusal(CapabilityCall("w-4", "kicad", {"action": "inspect"})))

    def test_the_runtime_checks_the_limit_before_her_broker(self) -> None:
        source = (ROOT / "src/alx/bootstrap/live_voice.py").read_text(encoding="utf-8")
        function = next(node for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.FunctionDef) and node.name == "worker_dispatch")
        rendered = ast.unparse(function)
        self.assertLess(rendered.index("worker_call_refusal(call)"), rendered.index("broker.dispatch"))

    def test_a_refused_call_reaches_the_worker_as_rejected(self) -> None:
        from alx.specialists.worker import MechanicalWorker
        dispatched = []

        def dispatch(call):
            refusal = worker_call_refusal(call)
            if refusal is not None:
                return refusal
            dispatched.append(call)
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, MAIL,
                                                      CapabilityResultState.SUCCEEDED, {}))

        worker = MechanicalWorker(object(), dispatch, DEFINITIONS)
        import json
        answer, summary = worker._call({"name": "call_capability", "input": {
            "capability_id": MAIL, "arguments_json": json.dumps(mail_arguments(TRASH, REFERENCE))}},
            "job-1")
        self.assertEqual(json.loads(answer)["disposition"], "rejected")
        self.assertEqual(summary["outcome"], "worker_action_not_allowed")
        self.assertEqual(dispatched, [])


class ReadSubjectLookupTests(unittest.TestCase):
    """The reply guard still finds the source subject from a mail read."""

    def attempt(self, action):
        return CapabilityAttempt(mail_call("r-1", action, REFERENCE),
                                 CapabilityAttemptDisposition.EXECUTED, True,
                                 CapabilityResult("r-1", MAIL, CapabilityResultState.SUCCEEDED,
                                                  {"subject": "Parts"}))

    def test_a_read_supplies_the_subject_and_another_action_does_not(self) -> None:
        conversation = ConversationSnapshot("c", (), 1, NOW + timedelta(days=1))
        send = CapabilityCall("s-1", "send_mail_reply", {**REFERENCE, "to": ["a@b"],
                                                         "subject": "RE: Parts", "body": ""})
        for action, unheard in ((READ, False), (SEARCH, True), (LIST_ATTACHMENTS, True)):
            with self.subTest(action=action):
                state = GoalState("goal-1", Objective("turn:1", "Reply"),
                                  (SuccessCriterion("c1", "sent"),), attempts=(self.attempt(action),))
                self.assertIs(CoreAgent._unheard_authored_text(conversation, send, state), unheard)

    def test_a_read_saved_before_d074_still_supplies_the_subject(self) -> None:
        """Review of c53ffdd: goals reload old calls unchanged."""
        from alx.goals.store import _goal_from_data, _goal_to_data
        old = CapabilityAttempt(CapabilityCall("r-0", "read_mail_message", dict(REFERENCE)),
                                CapabilityAttemptDisposition.EXECUTED, True,
                                CapabilityResult("r-0", "read_mail_message",
                                                 CapabilityResultState.SUCCEEDED, {"subject": "Parts"}))
        state = GoalState("goal-1", Objective("turn:1", "Reply"),
                          (SuccessCriterion("c1", "sent"),), attempts=(old,))
        reloaded = _goal_from_data("goal-1", _goal_to_data(state))
        conversation = ConversationSnapshot("c", (), 1, NOW + timedelta(days=1))
        send = CapabilityCall("s-1", "send_mail_reply", {**REFERENCE, "to": ["a@b"],
                                                         "subject": "RE: Parts", "body": ""})
        self.assertFalse(CoreAgent._unheard_authored_text(conversation, send, reloaded))

    def test_a_trash_saved_before_d074_is_still_known(self) -> None:
        from alx.contracts.mail import MailAction, recorded_mail_action
        self.assertIs(recorded_mail_action("move_mail_message_to_trash", {}), MailAction.TRASH)
        self.assertIs(recorded_mail_action("mail", {"action": "trash"}), MailAction.TRASH)
        self.assertIsNone(recorded_mail_action("mail", {"action": "nope"}))
        self.assertIsNone(recorded_mail_action("kicad", {"action": "read"}))


if __name__ == "__main__":
    unittest.main()
