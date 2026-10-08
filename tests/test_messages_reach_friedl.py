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


def _server(session=None, pending=None, relayed=None, relay=None,
            expires=None):
    def default_relay(source, turn, target):
        if relayed is not None:
            relayed.append((source, turn, target))
        return True

    server = LiveVoiceServer(session or _Session(), "127.0.0.1", 0, 16000, ROOT,
                             relay=relay or default_relay,
                             locate=lambda conversation, text: (f"turn-{text}", expires),
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
        self.assertEqual(relayed, [(MAIL, "turn-JLCPCB needs a choice", FRIEDL)])

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
            waiting = server._take_waiting(listener)
            await server._record_waiting(waiting, FRIEDL)
            return _drain(listener)

        self.assertEqual(asyncio.run(scenario()), ["first", "second"])
        self.assertEqual(relayed, [(MAIL, "turn-first", FRIEDL),
                                   ("mail-thread:<other>", "turn-second", FRIEDL)])
        self.assertEqual(self.pending.count(), 0)

    def test_a_waiting_message_expires_with_mail_content(self) -> None:
        self.pending.add(MAIL, "t", "old", NOW - timedelta(days=31))
        self.assertEqual(self.pending.take_all(NOW), ())

    def test_it_never_outlives_the_reply_it_came_from(self) -> None:
        server = _server(pending=self.pending, expires=NOW + timedelta(hours=2))
        server.deliver(MAIL, "soon gone")
        self.assertEqual(self.pending.take_all(NOW + timedelta(hours=3)), ())

    def test_unspoken_replies_go_back_to_waiting_when_the_session_closes(self) -> None:
        server = _server(pending=self.pending)
        server.deliver(MAIL, "first")
        listener: asyncio.Queue[str] = asyncio.Queue()
        handed = server._take_waiting(listener)
        server._keep_unspoken(listener, FRIEDL, handed)
        self.assertEqual(self.pending.take_all(NOW),
                         ((MAIL, "turn-first", "first", NOW + timedelta(days=30)),))

    def test_identical_unspoken_replies_keep_their_own_origins(self) -> None:
        self.pending.add(MAIL, "t1", "Same words", NOW)
        self.pending.add("mail-thread:<other>", "t2", "Same words", NOW)
        server = _server(pending=self.pending)
        listener: asyncio.Queue[str] = asyncio.Queue()
        server._keep_unspoken(listener, FRIEDL, server._take_waiting(listener))
        self.assertEqual([(source, turn) for source, turn, _t, _e in self.pending.take_all(NOW)],
                         [(MAIL, "t1"), ("mail-thread:<other>", "t2")])

    def test_a_reply_handed_back_keeps_its_original_deadline(self) -> None:
        deadline = NOW + timedelta(hours=2)
        server = _server(pending=self.pending, expires=deadline)
        server.deliver(MAIL, "first")
        later = NOW + timedelta(hours=1)
        server._now = lambda: later
        for _ in range(3):  # sessions keep closing before it is spoken
            listener: asyncio.Queue[str] = asyncio.Queue()
            server._keep_unspoken(listener, FRIEDL, server._take_waiting(listener))
        self.assertEqual(self.pending.take_all(later)[0][3], deadline)
        self.assertEqual(self.pending.take_all(deadline + timedelta(minutes=1)), ())

    def test_a_failed_copy_is_kept_and_made_later(self) -> None:
        attempts = []

        def flaky(source, turn, target):
            attempts.append(turn)
            if len(attempts) == 1:
                raise RuntimeError("database locked")
            return True

        server = _server(pending=self.pending, relay=flaky)
        server._relay_into(MAIL, ("t1", None), FRIEDL)
        self.assertEqual(len(self.pending.relays(NOW)), 1)
        server._retry_relays()
        self.assertEqual((attempts, self.pending.relays(NOW)), (["t1", "t1"], ()))

    def test_waiting_replies_are_queued_before_the_session_takes_live_ones(self) -> None:
        async def scenario():
            server = _server(pending=self.pending, relayed=[])
            server.deliver(MAIL, "older")
            listener: asyncio.Queue[str] = asyncio.Queue()
            server._take_waiting(listener)
            server._delivery_queues[FRIEDL] = [listener]
            server._live_conversations.append(FRIEDL)
            server._delivery_loop = asyncio.get_running_loop()
            await asyncio.to_thread(server.deliver, MAIL, "newer")
            return _drain(listener)

        self.assertEqual(asyncio.run(scenario()), ["older", "newer"])


class SetupFailureTests(unittest.TestCase):
    def test_a_connection_lost_during_setup_leaves_no_listener(self) -> None:
        from types import SimpleNamespace

        class Broken:
            request = SimpleNamespace(path=f"/voice?conversation_id={FRIEDL}")

            async def send(self, _message):
                raise ConnectionError("gone")

            async def close(self, **_kwargs):
                return None

        server = _server()

        async def scenario():
            with self.assertRaises(ConnectionError):
                await server._handle_voice(Broken())

        asyncio.run(scenario())
        self.assertEqual((server._live_conversations, server._delivery_queues), ([], {}))
        self.assertIs(server.deliver(MAIL, "later"), ResponseDelivery.UNDELIVERABLE)


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
        turn_id, _ = self.gateway.locate_reply(MAIL, "JLCPCB needs a choice")
        self.assertTrue(self.gateway.relay_response(MAIL, turn_id, FRIEDL))
        mine = self.store.load(FRIEDL)
        self.assertEqual([(t.turn_id, t.origin, t.content) for t in mine.turns],
                         [(f"relayed:{MAIL}#t1", ConversationOrigin.ALX_RESPONSE,
                           "JLCPCB needs a choice")])
        source = self.store.load(MAIL).turns[0]
        self.assertEqual(mine.turns[0].provenance, source.provenance)

    def test_it_is_recorded_once(self) -> None:
        self.gateway.relay_response(MAIL, "t1", FRIEDL)
        self.gateway.relay_response(MAIL, "t1", FRIEDL)
        self.assertEqual(len(self.store.load(FRIEDL).turns), 1)

    def test_identical_replies_keep_their_own_identity(self) -> None:
        keep = NOW + timedelta(days=30)
        snapshot = self.store.load(MAIL)
        provenance = RetentionPolicy().non_mail(ContentOrigin.ALX, NOW)
        self.store.append(ConversationTurn(MAIL, "t2", ConversationOrigin.ALX_RESPONSE,
                                           "JLCPCB needs a choice", NOW, provenance=provenance),
                          keep, snapshot.revision)
        self.gateway.relay_response(MAIL, "t1", FRIEDL)
        self.gateway.relay_response(MAIL, "t2", FRIEDL)
        self.assertEqual([t.turn_id for t in self.store.load(FRIEDL).turns],
                         [f"relayed:{MAIL}#t1", f"relayed:{MAIL}#t2"])

    def test_a_reply_not_in_its_thread_is_not_invented(self) -> None:
        self.assertFalse(self.gateway.relay_response(MAIL, "t9", FRIEDL))
        self.assertFalse(self.gateway.relay_response("mail-thread:<none>", "t1", FRIEDL))
        self.assertIsNone(self.gateway.locate_reply(MAIL, "something else"))


if __name__ == "__main__":
    unittest.main()
