"""Friedl's phone by iMessage (D-049).

He talks to AL/X from his phone when he is not at his screen, and what she
would say to him while he is away reaches his phone instead of waiting.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.continuity import ResponseDelivery  # noqa: E402
from alx.contracts.phone import IncomingMessage, MessagesUnavailable  # noqa: E402
from alx.interfaces.phone import PHONE_CONVERSATION_ID, PhoneLink  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402
from alx.providers.imessage import IMessageChannel  # noqa: E402

FRIEDL = "+27820000000"
ASSETS = Path(__file__).resolve().parents[1] / "src/alx/interfaces/assets"


def archived(text: str) -> bytes:
    """An attributedBody as Messages archives it, for a short text."""
    data = text.encode()
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@NSString\x01\x94\x84\x01+" + bytes(
        [len(data)]) + data + b"\x86\x84"


def messages_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB,
                              date INTEGER, is_from_me INTEGER, handle_id INTEGER,
                              cache_roomnames TEXT);
        INSERT INTO handle VALUES (1, '+27820000000'), (2, 'someone@else.com');
    """)
    rows = [
        (1, "before AL/X could read", None, 0, 0, 1, None),
        (2, "Are the readers ready?", None, 0, 0, 1, None),
        (3, None, archived("Back in an hour"), 0, 0, 1, None),
        (4, "from her, not him", None, 0, 1, 1, None),
        (5, "someone else", None, 0, 0, 2, None),
        (6, "in a group chat", None, 0, 0, 1, "chat123"),
    ]
    connection.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


class ChannelTests(unittest.TestCase):
    def test_only_his_own_one_to_one_messages_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.db"
            messages_database(path)
            channel = IMessageChannel(FRIEDL, path)
            self.assertEqual(channel.latest_row(), 6)
            found = channel.received_after(1)
        self.assertEqual([(item.row, item.text) for item in found],
                         [(2, "Are the readers ready?"), (3, "Back in an hour")])

    def test_an_unreadable_database_says_so(self) -> None:
        channel = IMessageChannel(FRIEDL, Path("/nonexistent/chat.db"))
        with self.assertRaises(MessagesUnavailable) as caught:
            channel.received_after(0)
        self.assertEqual(caught.exception.code, "read_denied")

    def test_the_text_is_an_argument_never_part_of_the_script(self) -> None:
        calls = []

        def runner(arguments, **keywords):
            calls.append(arguments)
            return SimpleNamespace(returncode=0)

        IMessageChannel(FRIEDL, runner=runner).send('Done" & do shell script "x')
        script, text, handle = calls[0][2], calls[0][3], calls[0][4]
        self.assertNotIn("do shell script", script)
        self.assertEqual((text, handle), ('Done" & do shell script "x', FRIEDL))

    def test_a_failed_send_raises(self) -> None:
        for outcome in (SimpleNamespace(returncode=1),
                        subprocess.TimeoutExpired("osascript", 30)):
            def runner(arguments, outcome=outcome, **keywords):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            with self.subTest(outcome=type(outcome).__name__):
                with self.assertRaises(MessagesUnavailable):
                    IMessageChannel(FRIEDL, runner=runner).send("hello")


class FakeChannel:
    def __init__(self, arrived=(), latest=10, fail_send=False):
        self.arrived = list(arrived)
        self.latest = latest
        self.sent: list[str] = []
        self.fail_send = fail_send

    def latest_row(self):
        return self.latest

    def received_after(self, row):
        return [item for item in self.arrived if item.row > row]

    def send(self, text):
        if self.fail_send:
            raise MessagesUnavailable("send_failed")
        self.sent.append(text)


class LinkTests(unittest.TestCase):
    def link(self, channel, presence="here", response="Yes, both are ready."):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.turns = []

        async def remote_turn(conversation_id, content):
            self.turns.append(SimpleNamespace(conversation_id=conversation_id, content=content))
            return SimpleNamespace(response=response)

        session = SimpleNamespace(remote_turn=remote_turn)
        return PhoneLink(channel, session, Path(directory.name) / "cursor.json",
                         lambda: (presence, None))

    def message(self, row, text):
        return IncomingMessage(row, text, datetime(2026, 10, 15, tzinfo=UTC))

    def test_the_first_start_answers_nothing_from_before(self) -> None:
        channel = FakeChannel([self.message(9, "old")], latest=9)
        link = self.link(channel)
        self.assertEqual(asyncio.run(link.poll_once()), 0)
        self.assertEqual((self.turns, channel.sent), ([], []))

    def test_his_message_is_a_turn_in_the_phone_thread_and_answered_there(self) -> None:
        channel = FakeChannel(latest=10)
        link = self.link(channel)
        asyncio.run(link.poll_once())
        channel.arrived.append(self.message(11, "Are the readers ready?"))
        self.assertEqual(asyncio.run(link.poll_once()), 1)
        self.assertEqual(self.turns[0].conversation_id, PHONE_CONVERSATION_ID)
        self.assertEqual(self.turns[0].content, "Are the readers ready?")
        self.assertEqual(channel.sent, ["Yes, both are ready."])
        # Not answered twice.
        self.assertEqual(asyncio.run(link.poll_once()), 0)

    def test_an_answer_that_cannot_be_sent_takes_the_ordinary_path(self) -> None:
        channel = FakeChannel(latest=10, fail_send=True)
        link = self.link(channel)
        kept = []
        link.fallback = kept.append
        asyncio.run(link.poll_once())
        channel.arrived.append(self.message(11, "Status?"))
        asyncio.run(link.poll_once())
        self.assertEqual(kept, ["Yes, both are ready."])

    def test_what_goes_to_his_phone(self) -> None:
        self.assertTrue(self.link(FakeChannel(), "away").carries("any-thread"))
        self.assertFalse(self.link(FakeChannel(), "here").carries("any-thread"))
        self.assertTrue(self.link(FakeChannel(), "here").carries(PHONE_CONVERSATION_ID))


class RemoteTurnTests(unittest.TestCase):
    def test_his_message_is_the_same_turn_a_typed_line_makes(self) -> None:
        from alx.contracts.records import ConversationOrigin
        from alx.interfaces.live_voice import VoiceSession

        seen = []

        def receive(turn, budget, retention):
            seen.append((turn, budget))
            return SimpleNamespace(response="ok")

        session = VoiceSession(SimpleNamespace(receive_conversation_turn=receive),
                               Mock(), None, "friedl", 8, 30)
        outcome = asyncio.run(session.remote_turn(PHONE_CONVERSATION_ID, "Status?"))
        turn, budget = seen[0]
        self.assertEqual(outcome.response, "ok")
        self.assertEqual((turn.conversation_id, turn.origin, turn.content, turn.person_id, budget),
                         (PHONE_CONVERSATION_ID, ConversationOrigin.TYPED, "Status?", "friedl", 8))


class DeliveryTests(unittest.TestCase):
    def server(self, phone, pending):
        return LiveVoiceServer(Mock(), "127.0.0.1", 8765, 16000, ASSETS,
                               pending=pending, phone=phone)

    def phone(self, carries=True, sends=True):
        phone = Mock()
        phone.carries.return_value = carries
        phone.send.return_value = sends
        return phone

    def test_while_he_is_away_it_goes_to_his_phone(self) -> None:
        phone, pending = self.phone(), Mock()
        self.assertIs(self.server(phone, pending).deliver("mail-thread:x", "DHL invoice in."),
                      ResponseDelivery.DELIVERED)
        phone.send.assert_called_once_with("DHL invoice in.")
        pending.add.assert_not_called()

    def test_if_it_cannot_be_sent_it_waits_as_before(self) -> None:
        phone, pending = self.phone(sends=False), Mock()
        self.assertIs(self.server(phone, pending).deliver("mail-thread:x", "DHL invoice in."),
                      ResponseDelivery.QUEUED)
        pending.add.assert_called_once()

    def test_while_he_is_here_his_phone_is_left_alone(self) -> None:
        phone, pending = self.phone(carries=False), Mock()
        self.server(phone, pending).deliver("mail-thread:x", "DHL invoice in.")
        phone.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
