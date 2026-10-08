"""D-044: what AL/X wants to tell Friedl reaches him.

She thinks about each mail thread in its own conversation, and her reply used
to be offered only to a listener on that thread, which no browser ever is: on
2026-10-08 every message about the morning's mail was recorded as undelivered
while Friedl's session was open all along. These tests prove a reply made in
any thread reaches the session he has open and is recorded in his thread, a
reply made while nobody is connected waits and is handed to his next session
in order, a waiting reply is not reported as unheard, and nothing else about
delivery changes.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.continuity.pending_messages import SQLitePendingMessages  # noqa: E402
from alx.contracts import ConversationOrigin, ConversationTurn, ResponseDelivery  # noqa: E402
from alx.contracts.provenance import ContentOrigin, RetentionPolicy  # noqa: E402
from alx.conversation.gateway import ConversationGateway  # noqa: E402
from alx.conversation.store import SQLiteConversationStore  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402

NOW = datetime(2026, 10, 8, 11, 53, tzinfo=UTC)
MAIL = "mail-thread:<jlcpcb-123@mail>"
FRIEDL = "6f528a72-0933-442c-be31-25778141a08a"


class _Session:
    def __init__(self, busy=()):
        self.busy = set(busy)

    def admits_unprompted_speech(self, conversation_id):
        return conversation_id not in self.busy


def _server(session=None, pending=None, relayed=None):
    server = LiveVoiceServer(session or _Session(), "127.0.0.1", 0, 16000, ROOT,
                             relay=lambda s, t, x: (relayed.append((s, t, x)) or True)
                             if relayed is not None else True,
                             pending=pending, clock=lambda: NOW)
    return server


def _connect(server, conversation_id, loop):
    listener: asyncio.Queue[str] = asyncio.Queue()
    server._delivery_queues.setdefault(conversation_id, []).append(listener)
    server._live_conversations.append(conversation_id)
    server._delivery_loop = loop
    return listener


def _drain(listener):
    items = []
    while not listener.empty():
        items.append(listener.get_nowait())
    return items


class RoutingTests(unittest.TestCase):
    def test_a_mail_thread_reply_reaches_friedls_open_session(self) -> None:
        relayed = []

        async def scenario():
            server = _server(relayed=relayed)
            mine = _connect(server, FRIEDL, asyncio.get_running_loop())
            result = await asyncio.to_thread(server.deliver, MAIL, "JLCPCB needs a choice")
            return result, _drain(mine)

        result, heard = asyncio.run(scenario())
        self.assertIs(result, ResponseDelivery.DELIVERED)
        self.assertEqual(heard, ["JLCPCB needs a choice"])
        self.assertEqual(relayed, [(MAIL, FRIEDL, "JLCPCB needs a choice")])

    def test_a_reply_in_his_own_thread_is_not_copied(self) -> None:
        relayed = []

        async def scenario():
            server = _server(relayed=relayed)
            mine = _connect(server, FRIEDL, asyncio.get_running_loop())
            result = await asyncio.to_thread(server.deliver, FRIEDL, "hello")
            return result, _drain(mine)

        result, heard = asyncio.run(scenario())
        self.assertEqual((result, heard, relayed), (ResponseDelivery.DELIVERED, ["hello"], []))

    def test_the_newest_open_session_hears_it(self) -> None:
        async def scenario():
            server = _server(relayed=[])
            older = _connect(server, "older", asyncio.get_running_loop())
            newer = _connect(server, FRIEDL, asyncio.get_running_loop())
            await asyncio.to_thread(server.deliver, MAIL, "news")
            return _drain(older), _drain(newer)

        self.assertEqual(asyncio.run(scenario()), ([], ["news"]))

    def test_his_pending_turn_still_owns_the_voice(self) -> None:
        relayed = []

        async def scenario():
            server = _server(session=_Session(busy={FRIEDL}), relayed=relayed)
            mine = _connect(server, FRIEDL, asyncio.get_running_loop())
            result = await asyncio.to_thread(server.deliver, MAIL, "news")
            return result, _drain(mine)

        result, heard = asyncio.run(scenario())
        self.assertEqual((result, heard, relayed), (ResponseDelivery.UNDELIVERABLE, [], []))


class WaitingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.pending = SQLitePendingMessages(Path(directory.name) / "pending.sqlite3")
        self.addCleanup(self.pending.close)

    def test_with_nobody_connected_it_waits(self) -> None:
        server = _server(pending=self.pending)
        self.assertIs(server.deliver(MAIL, "JLCPCB needs a choice"), ResponseDelivery.QUEUED)
        self.assertEqual(self.pending.count(), 1)

    def test_without_a_store_nothing_changes(self) -> None:
        self.assertIs(_server().deliver(MAIL, "x"), ResponseDelivery.UNDELIVERABLE)

    def test_his_next_session_hears_what_waited_in_order(self) -> None:
        relayed = []
        server = _server(pending=self.pending, relayed=relayed)
        server.deliver(MAIL, "first")
        server.deliver("mail-thread:<other>", "second")

        async def scenario():
            listener: asyncio.Queue[str] = asyncio.Queue()
            await server._hand_over_pending(FRIEDL, listener)
            return _drain(listener)

        self.assertEqual(asyncio.run(scenario()), ["first", "second"])
        self.assertEqual([item[:2] for item in relayed],
                         [(MAIL, FRIEDL), ("mail-thread:<other>", FRIEDL)])
        self.assertEqual(self.pending.count(), 0)

    def test_a_waiting_message_expires_with_mail_content(self) -> None:
        self.pending.add(MAIL, "old", NOW - timedelta(days=31))
        self.assertEqual(self.pending.take_all(NOW), ())


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteConversationStore(Path(directory.name) / "c.sqlite3")
        self.gateway = ConversationGateway(object(), self.store, clock=lambda: NOW)
        keep = NOW + timedelta(days=30)
        mail = self.store.create(MAIL, keep)
        provenance = RetentionPolicy().non_mail(ContentOrigin.ALX, NOW)
        self.store.append(ConversationTurn(MAIL, "t1", ConversationOrigin.ALX_RESPONSE,
                                           "JLCPCB needs a choice", NOW, provenance=provenance),
                          keep, mail.revision)

    def test_the_reply_is_recorded_in_his_thread_with_its_provenance(self) -> None:
        self.assertTrue(self.gateway.relay_response(MAIL, FRIEDL, "JLCPCB needs a choice"))
        mine = self.store.load(FRIEDL)
        self.assertEqual([(t.turn_id, t.origin, t.content) for t in mine.turns],
                         [("relayed:t1", ConversationOrigin.ALX_RESPONSE,
                           "JLCPCB needs a choice")])
        source = self.store.load(MAIL).turns[0]
        self.assertEqual(mine.turns[0].provenance, source.provenance)

    def test_it_is_recorded_once(self) -> None:
        self.gateway.relay_response(MAIL, FRIEDL, "JLCPCB needs a choice")
        self.gateway.relay_response(MAIL, FRIEDL, "JLCPCB needs a choice")
        self.assertEqual(len(self.store.load(FRIEDL).turns), 1)

    def test_a_reply_not_in_its_thread_is_not_invented(self) -> None:
        self.assertFalse(self.gateway.relay_response(MAIL, FRIEDL, "something else"))
        self.assertFalse(self.gateway.relay_response("mail-thread:<none>", FRIEDL, "x"))


if __name__ == "__main__":
    unittest.main()
