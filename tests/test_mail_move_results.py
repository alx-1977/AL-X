"""A server move and local mail attention are independent facts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityResultState, Evidence, GoalMutationKind, GoalState,
    MailAccessError, Objective, SuccessCriterion,
)
from alx.core import CoreAgent  # noqa: E402
from alx.providers.icloud_mail import ICloudMailAdapter, SQLiteMailObservationState  # noqa: E402
from alx.tools import (  # noqa: E402
    FILE_PROCESSED_MAIL_MESSAGE, MOVE_MAIL_MESSAGE_TO_TRASH, build_mail_executors,
)


SOURCE = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "42"}
DESTINATION = {"mailbox_id": "Processed", "uid_validity": "888", "uid": "7"}
MESSAGE_ID = "<invoice@example.test>"
MESSAGE_BYTES = (
    f"Message-ID: {MESSAGE_ID}\r\n"
    "Subject: Invoice\r\n"
    "\r\n"
).encode()


class MoveConnection:
    def __init__(self) -> None:
        self.selected = ""
        self.commands = []
        self.move_status = "OK"
        self.move_error = None
        self.copyuid = b"888 42 7"
        self.move_data = [b"Move completed"]
        self.destination_select = "OK"
        self.destination_validity = "888"
        # COPYUID read-back. Distinct from the post-move source and Message-ID
        # searches so a canned hit here cannot pretend the presence check passed.
        self.destination_search = "OK", [b"7"]
        self.message_bytes = None
        self.source_search = "NO", []
        self.header_search = "NO", []

    def login(self, *_):
        return "OK", []

    def logout(self):
        return "BYE", []

    def select(self, mailbox, readonly=False):
        self.selected = mailbox.strip('"')
        self.commands.append(("SELECT", self.selected, readonly))
        if self.selected != "INBOX":
            return self.destination_select, [b"1"]
        return "OK", [b"1"]

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [self.destination_validity.encode() if self.selected != "INBOX" else b"777"]
        if name == "COPYUID":
            return name, [self.copyuid] if self.copyuid is not None else [None]
        raise AssertionError(name)

    def list(self):
        return "OK", [b'(\\Trash) "/" "Processed"']

    def uid(self, operation, *arguments):
        self.commands.append(("UID", operation, *arguments))
        if operation == "MOVE":
            if self.move_error is not None:
                raise self.move_error
            return self.move_status, self.move_data
        if operation == "fetch":
            if self.message_bytes is None:
                return "NO", []
            return "OK", [(b"metadata", self.message_bytes), b")"]
        if operation == "search":
            if "HEADER" in arguments:
                return self.header_search
            if arguments[-2:] == ("UID", SOURCE["uid"]):
                return self.source_search
            return self.destination_search
        raise AssertionError(operation)


class MoveResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.observations = SQLiteMailObservationState(
            Path(self.temporary.name) / "observations.sqlite3"
        )
        self.addCleanup(self.observations.close)
        self.connection = MoveConnection()
        self.account = ICloudMailAdapter(
            "imap.test", 993, "person@test", "secret", self.observations, 15,
            connection_factory=lambda *_, **__: self.connection,
        )
        self.executors = build_mail_executors(
            self.account, self.account, lambda: "call-1", processed_mailbox="Processed"
        )

    def run_move(self, capability):
        return self.executors[capability](SOURCE)

    def settle_observation(self):
        with self.observations._connection:
            self.observations._connection.execute(
                "INSERT INTO mail_observations "
                "(mailbox_id, uid_validity, uid, event_json, state) "
                "VALUES ('INBOX', '777', 42, '{}', 'done')"
            )

    def assert_one_move(self):
        self.assertEqual(
            len([item for item in self.connection.commands if item[:2] == ("UID", "MOVE")]),
            1,
        )

    def test_confirmed_move_with_done_or_absent_observation_for_both_capabilities(self):
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            for settled in (False, True):
                with self.subTest(capability=capability, settled=settled):
                    self._reset()
                    if settled:
                        self.settle_observation()
                    result = self.run_move(capability)
                    self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
                    self.assertEqual(result.values["destination_reference"], DESTINATION)
                    self.assertTrue(result.values["moved" if capability == MOVE_MAIL_MESSAGE_TO_TRASH else "filed"])
                    self.assertIn(("SELECT", "Processed", True), self.connection.commands)
                    self.assertIn(("UID", "search", None, "UID", "7"), self.connection.commands)
                    self.assert_one_move()

    def test_missing_or_malformed_copyuid_is_partial_for_both_capabilities(self):
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            for value in (None, b"bad", b"888 41 7", b"0 42 7"):
                with self.subTest(capability=capability, value=value):
                    self._reset()
                    self.connection.copyuid = value
                    result = self.run_move(capability)
                    self.assertEqual(result.state, CapabilityResultState.PARTIAL)
                    self.assertEqual(result.failure["code"], "mail_move_unconfirmed")
                    self.assertNotIn("destination_reference", result.values)
                    self.assert_one_move()

    def test_destination_read_back_unavailable_is_partial_for_both_capabilities(self):
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            for unavailable in ("select", "validity", "search", "absent"):
                with self.subTest(capability=capability, unavailable=unavailable):
                    self._reset()
                    if unavailable == "select":
                        self.connection.destination_select = "NO"
                    elif unavailable == "validity":
                        self.connection.destination_validity = "889"
                    elif unavailable == "search":
                        self.connection.destination_search = "NO", []
                    else:
                        self.connection.destination_search = "OK", [b""]
                    result = self.run_move(capability)
                    self.assertEqual(result.state, CapabilityResultState.PARTIAL)
                    self.assertEqual(result.failure["code"], "mail_move_unconfirmed")
                    self.assert_one_move()

    def test_copyuid_in_move_response_is_used_when_response_code_is_unavailable(self):
        self.connection.copyuid = None
        self.connection.move_data = [b"[COPYUID 888 42 7] Move completed"]
        result = self.run_move(FILE_PROCESSED_MAIL_MESSAGE)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["destination_reference"], DESTINATION)

    def test_rejected_uid_move_is_definite_failure_for_both_capabilities(self):
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            with self.subTest(capability=capability):
                self._reset()
                self.connection.move_status = "NO"
                result = self.run_move(capability)
                self.assertEqual(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure["code"], "move_failed")
                self.assert_one_move()

    def _unavailable_observation(self) -> None:
        self.observations.acknowledge = lambda _reference: (_ for _ in ()).throw(
            MailAccessError("observation_unavailable")
        )

    def _arm_presence(self, source, header, *, lost: bool = False) -> None:
        """COPYUID is absent. Only the post-move check can decide the outcome."""
        self.connection.copyuid = None
        self.connection.message_bytes = MESSAGE_BYTES
        self.connection.source_search = source
        self.connection.header_search = header
        if lost:
            self.connection.move_error = OSError("reset")
        self._unavailable_observation()

    def test_presence_confirms_a_move_copyuid_and_observation_cannot(self):
        quoted = f'"{MESSAGE_ID}"'
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            for lost in (False, True):
                with self.subTest(capability=capability, lost=lost):
                    self._reset()
                    self._arm_presence(("OK", [b""]), ("OK", [b"7"]), lost=lost)
                    result = self.run_move(capability)
                    self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
                    self.assertIsNone(result.failure)
                    self.assertEqual(result.values["destination_reference"], DESTINATION)
                    self.assertTrue(
                        result.values["moved" if capability == MOVE_MAIL_MESSAGE_TO_TRASH else "filed"]
                    )
                    operations = [
                        item[1] for item in self.connection.commands if item[0] == "UID"
                    ]
                    self.assertLess(operations.index("fetch"), operations.index("MOVE"))
                    self.assertLess(operations.index("MOVE"), operations.index("search"))
                    self.assertIn(
                        ("UID", "search", None, "HEADER", "Message-ID", quoted),
                        self.connection.commands,
                    )
                    self.assert_one_move()

    def test_presence_failure_is_move_failed_for_both_capabilities(self):
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            for lost in (False, True):
                with self.subTest(capability=capability, lost=lost):
                    self._reset()
                    self._arm_presence(("OK", [b"42"]), ("OK", [b""]), lost=lost)
                    result = self.run_move(capability)
                    self.assertEqual(result.state, CapabilityResultState.FAILED)
                    self.assertEqual(result.failure["code"], "move_failed")
                    self.assertNotIn("destination_reference", result.values)
                    self.assert_one_move()

    def test_inconclusive_presence_stays_unconfirmed_for_both_capabilities(self):
        shapes = {
            "unknown": (("NO", []), ("NO", [])),
            "left_without_match": (("OK", [b""]), ("OK", [b""])),
            "several_matches": (("OK", [b""]), ("OK", [b"7 9"])),
        }
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            for lost in (False, True):
                for name, (source, header) in shapes.items():
                    with self.subTest(capability=capability, lost=lost, shape=name):
                        self._reset()
                        self._arm_presence(source, header, lost=lost)
                        result = self.run_move(capability)
                        self.assertEqual(result.state, CapabilityResultState.PARTIAL)
                        self.assertEqual(result.failure["code"], "mail_move_unconfirmed")
                        self.assertNotIn("destination_reference", result.values)
                        self.assert_one_move()

    def test_local_acknowledge_error_does_not_downgrade_confirmed_move(self):
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            with self.subTest(capability=capability):
                self._reset()
                self.observations.acknowledge = lambda _reference: (_ for _ in ()).throw(
                    MailAccessError("observation_unavailable")
                )
                self.assertEqual(self.run_move(capability).state, CapabilityResultState.SUCCEEDED)
                self.assert_one_move()

    def test_only_confirmed_move_can_support_completion_for_both_capabilities(self):
        for capability in (MOVE_MAIL_MESSAGE_TO_TRASH, FILE_PROCESSED_MAIL_MESSAGE):
            for confirmed in (False, True):
                with self.subTest(capability=capability, confirmed=confirmed):
                    self._reset()
                    if not confirmed:
                        self.connection.copyuid = None
                    result = self.run_move(capability)
                    attempt = CapabilityAttempt(
                        CapabilityCall("call-1", capability, SOURCE),
                        CapabilityAttemptDisposition.EXECUTED, True, result,
                    )
                    state = GoalState(
                        goal_id="goal-1", objective=Objective("turn:turn-1", "Move mail"),
                        success_criteria=(SuccessCriterion("filed", "mail filed"),),
                        attempts=(attempt,),
                        evidence=(Evidence("move-evidence", "mail_move", supports=("filed",),
                                           source_references=("attempt:call-1",)),),
                    )
                    if confirmed:
                        self.assertEqual(
                            CoreAgent._derive_goal_status(state, GoalMutationKind.REQUEST_COMPLETION).status.value,
                            "completed",
                        )
                    else:
                        with self.assertRaisesRegex(ValueError, "completion_lacks_sourced_evidence"):
                            CoreAgent._derive_goal_status(state, GoalMutationKind.REQUEST_COMPLETION)


class CopyUidParsingTests(unittest.TestCase):
    def test_response_code_and_move_response_map_the_exact_source_uid(self):
        parse = ICloudMailAdapter._copyuid
        self.assertEqual(parse([b"888 42 7"], "42", response_code=True), ("888", "7"))
        self.assertEqual(parse([b"[COPYUID 888 42 7] Move completed"], "42"), ("888", "7"))
        self.assertIsNone(parse([b"888 41 7"], "42", response_code=True))
        self.assertIsNone(parse([b"888 42:43 7:8"], "42", response_code=True))
        self.assertIsNone(parse([b"0 42 7"], "42", response_code=True))
        self.assertIsNone(parse([b"888 42 999999999999"], "42", response_code=True))
        self.assertIsNone(parse([b"888 42 7"], "9" * 5000, response_code=True))
