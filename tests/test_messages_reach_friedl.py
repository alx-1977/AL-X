"""Delivered replies reach Friedl; unheard occasions reach fresh Core reasoning."""

from __future__ import annotations

import asyncio
import ast
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.continuity.pending_relays import SQLitePendingRelays  # noqa: E402
from alx.core import CoreOutcome, CoreState  # noqa: E402
from alx.contracts.core import ReasoningContext  # noqa: E402
from alx.core.model_reasoner import _context_payload  # noqa: E402
from alx.contracts import AudioChunk, ConversationOrigin, ConversationTurn, ResponseDelivery  # noqa: E402
from alx.contracts.provenance import ContentOrigin, RetentionPolicy  # noqa: E402
from alx.conversation.gateway import ConversationGateway  # noqa: E402
from alx.conversation.store import SQLiteConversationStore  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402
from alx.interfaces.live_voice import VoiceActivityStatus, VoiceEventKind, VoiceSession  # noqa: E402

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

    def test_a_reply_in_his_own_thread_confirms_its_original_identity(self) -> None:
        relayed = []

        async def scenario():
            server = _server(relayed=relayed)
            mine = _connect(server, FRIEDL, asyncio.get_running_loop())
            result = await asyncio.to_thread(server.deliver, FRIEDL, "hello")
            return result, _drain(mine)

        result, heard = asyncio.run(scenario())
        self.assertEqual((result, heard), (ResponseDelivery.DELIVERED, ["hello"]))
        self.assertEqual(relayed, [(FRIEDL, "turn-hello", FRIEDL)])

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

    def test_with_no_session_the_reply_is_undeliverable(self) -> None:
        self.assertIs(_server().deliver(MAIL, "stale words"),
                      ResponseDelivery.UNDELIVERABLE)


class SessionOpenTests(unittest.TestCase):
    def test_close_during_speech_start_send_preserves_autonomous_occasion(self) -> None:
        from types import SimpleNamespace

        marked = []

        class Connection:
            request = SimpleNamespace(path=f"/voice?conversation_id={FRIEDL}")

            def __init__(self):
                self.ready = asyncio.Event()

            async def send(self, frame):
                if isinstance(frame, str):
                    message = json.loads(frame)
                    if message.get("type") == "session.ready":
                        self.ready.set()
                    if message.get("value") == "speaking":
                        raise ConnectionError("closed before speech started")

            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b""

        class Transcriber:
            async def transcribe(self, _audio):
                await asyncio.Event().wait()
                yield None

        class Synthesizer:
            async def synthesize(self, _response, _conversation_id):
                yield AudioChunk(b"sound", "audio/pcm")

        async def scenario():
            session = VoiceSession(object(), Transcriber(), Synthesizer(),
                                   "friedl", 3, 30)
            server = _server(session=session)
            server._mark_undelivered = marked.append
            connection = Connection()
            handling = asyncio.create_task(server._handle_voice(connection))
            await connection.ready.wait()
            result = await asyncio.to_thread(server.deliver, MAIL, "fresh", "occasion-1")
            await asyncio.wait_for(handling, timeout=2)
            return result

        self.assertIs(asyncio.run(scenario()), ResponseDelivery.DELIVERED)
        self.assertEqual(marked, ["occasion-1"])

    def test_progress_send_failure_keeps_core_owned_until_worker_finishes(self) -> None:
        working = threading.Event()
        release = threading.Event()
        remote_started = threading.Event()
        activity = VoiceActivityStatus()

        class Gateway:
            def receive_session_open(self, *_args):
                activity.set("coding")
                working.set()
                release.wait(timeout=3)
                return CoreOutcome(CoreState.RESPONDED, None, response="fresh")

            def receive_conversation_turn(self, *_args):
                remote_started.set()
                return CoreOutcome(CoreState.FINISHED_SILENTLY, None)

        async def scenario():
            session = VoiceSession(Gateway(), object(), object(), "friedl", 3, 30,
                                   activity=activity, undelivered=lambda: ({"id": "one"},))

            async def progress(event):
                if event.kind is VoiceEventKind.ACTIVITY:
                    raise ConnectionError("browser disconnected")

            opening = asyncio.create_task(session.session_open_response(FRIEDL, progress))
            await asyncio.to_thread(working.wait, 2)
            try:
                for _ in range(100):
                    if opening.done():
                        break
                    await asyncio.sleep(0.01)
                remote = asyncio.create_task(session.remote_turn(FRIEDL, "phone"))
                await asyncio.sleep(0.05)
                self.assertTrue(session._core_turn_lock.locked())
                self.assertFalse(remote_started.is_set())
            finally:
                release.set()
            with self.assertRaises(ConnectionError):
                await opening
            await remote
            self.assertTrue(remote_started.is_set())

        asyncio.run(scenario())

    def test_cancelled_session_open_keeps_core_owned_until_worker_finishes(self) -> None:
        working = threading.Event()
        release = threading.Event()
        remote_started = threading.Event()

        class Gateway:
            def receive_session_open(self, *_args):
                working.set()
                release.wait(timeout=3)
                return CoreOutcome(CoreState.RESPONDED, None, response="fresh")

            def receive_conversation_turn(self, *_args):
                remote_started.set()
                return CoreOutcome(CoreState.FINISHED_SILENTLY, None)

        async def scenario():
            session = VoiceSession(Gateway(), object(), object(), "friedl", 3, 30,
                                   undelivered=lambda: ({"id": "one"},))
            opening = asyncio.create_task(session.session_open_response(FRIEDL))
            await asyncio.to_thread(working.wait, 2)
            opening.cancel()
            remote = asyncio.create_task(session.remote_turn(FRIEDL, "phone"))
            try:
                await asyncio.sleep(0.05)
                self.assertTrue(session._core_turn_lock.locked())
                self.assertFalse(remote_started.is_set())
                opening.cancel()  # A second disconnect signal during cleanup.
                await asyncio.sleep(0.05)
                self.assertTrue(session._core_turn_lock.locked())
                self.assertFalse(remote_started.is_set())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await opening
            await remote
            self.assertTrue(remote_started.is_set())

        asyncio.run(scenario())

    def test_close_before_speech_restores_the_occasion_without_words(self) -> None:
        from types import SimpleNamespace

        marked = []

        class Connection:
            request = SimpleNamespace(path=f"/voice?conversation_id={FRIEDL}")

            async def send(self, _frame):
                return None

        class Session(_Session):
            async def session_open_response(self, _conversation_id, progress=None):
                return None

        async def scenario(start_speech, take_without_start=False):
            server = _server(session=Session())
            server._mark_undelivered = marked.append
            ready = asyncio.Event()
            close = asyncio.Event()

            async def exchange(_connection, _conversation_id):
                ready.set()
                await close.wait()
                if start_speech or take_without_start:
                    delivery = server._delivery_queue(FRIEDL).get_nowait()
                    if start_speech:
                        delivery.mark_started()
                return False

            server._exchange_once = exchange
            task = asyncio.create_task(server._handle_voice(Connection()))
            await ready.wait()
            result = await asyncio.to_thread(server.deliver, MAIL, "do not retain", "occasion-1")
            close.set()
            await task
            return result, server._delivery_queues

        result, queues = asyncio.run(scenario(False))
        self.assertIs(result, ResponseDelivery.DELIVERED)
        self.assertEqual(marked, ["occasion-1"])
        self.assertEqual(queues, {})

        marked.clear()
        result, _ = asyncio.run(scenario(False, take_without_start=True))
        self.assertIs(result, ResponseDelivery.DELIVERED)
        self.assertEqual(marked, ["occasion-1"])

        marked.clear()
        result, _ = asyncio.run(scenario(True))
        self.assertIs(result, ResponseDelivery.DELIVERED)
        self.assertEqual(marked, [])

    def test_slow_session_open_forwards_thinking_and_activity_live(self) -> None:
        from types import SimpleNamespace

        working = threading.Event()
        release = threading.Event()
        activity = VoiceActivityStatus()

        class Core:
            def process(self, conversation, retention, budget, **keywords):
                activity.set("coding")
                working.set()
                release.wait(timeout=2)
                return CoreOutcome(CoreState.RESPONDED, None, response="fresh words")

        class Connection:
            request = SimpleNamespace(path=f"/voice?conversation_id={FRIEDL}")

            def __init__(self):
                self.frames = []

            async def send(self, frame):
                self.frames.append(json.loads(frame))

        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                store = SQLiteConversationStore(Path(directory) / "conversation.sqlite3")
                gateway = ConversationGateway(Core(), store, clock=lambda: NOW)
                session = VoiceSession(gateway, object(), object(), "friedl", 3, 30,
                                       clock=lambda: NOW,
                                       undelivered=lambda: ({"opportunity_id": "one"},),
                                       activity=activity)
                server = _server(session=session)
                connection = Connection()

                async def exchange(_connection, _conversation_id):
                    self.assertEqual(server._delivery_queue(FRIEDL).get_nowait(),
                                     "fresh words")
                    return False

                server._exchange_once = exchange
                task = asyncio.create_task(server._handle_voice(connection))
                await asyncio.to_thread(working.wait, 2)
                for _ in range(100):
                    if any(frame.get("type") == "activity" for frame in connection.frames):
                        break
                    await asyncio.sleep(0.01)
                frames_while_working = list(connection.frames)
                release.set()
                await task
                store.close()
                return frames_while_working

        frames = asyncio.run(scenario())
        self.assertEqual(frames[0]["type"], "session.ready")
        self.assertIn({"type": "phase", "value": "thinking"}, frames)
        self.assertIn({"type": "activity", "value": "coding"}, frames)

    def test_server_hands_one_fresh_core_response_to_the_existing_voice_path(self) -> None:
        from types import SimpleNamespace

        class Session:
            calls = 0

            async def session_open_response(self, conversation_id, progress=None):
                self.calls += 1
                self.conversation_id = conversation_id
                return "newly composed"

        class Connection:
            request = SimpleNamespace(path=f"/voice?conversation_id={FRIEDL}")

            def __init__(self):
                self.frames = []

            async def send(self, frame):
                self.frames.append(frame)

        session = Session()
        server = _server(session=session)
        connection = Connection()

        async def observe(_connection, conversation_id):
            self.assertEqual(conversation_id, FRIEDL)
            self.assertEqual(server._delivery_queue(FRIEDL).get_nowait(),
                             "newly composed")
            return False

        server._exchange_once = observe
        asyncio.run(server._handle_voice(connection))
        self.assertEqual(session.calls, 1)
        self.assertEqual((server._delivery_queues, server._live_conversations),
                         ({}, []))

    def test_core_evidence_contains_references_but_no_saved_words(self) -> None:
        payload = json.loads(_context_payload(ReasoningContext(
            None, (), (), conversation_id=FRIEDL, undelivered_responses=(
                {"opportunity_id": "first", "origin": "external_event",
                 "arose_at": NOW.isoformat(), "refs": "mail:1",
                 "text": "outdated reply"},
            ),
        )))
        self.assertEqual(payload["undelivered_responses"][0]["references"], ["mail:1"])
        self.assertNotIn("outdated reply", json.dumps(payload))

    def test_one_core_turn_receives_the_occasion_evidence(self) -> None:
        occasions = ({"opportunity_id": "first", "refs": "mail:1"},
                     {"opportunity_id": "second", "refs": "mail:2"})
        calls = []

        class Core:
            def process(self, conversation, retention, budget, **keywords):
                calls.append((conversation, keywords))
                return CoreOutcome(CoreState.RESPONDED, None,
                                   response="A fresh question about one item")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteConversationStore(Path(directory) / "conversation.sqlite3")
            gateway = ConversationGateway(Core(), store, clock=lambda: NOW)
            session = VoiceSession(gateway, object(), object(), "friedl", 3, 30,
                                   clock=lambda: NOW, undelivered=lambda: occasions)
            self.assertEqual(asyncio.run(session.session_open_response(FRIEDL)),
                             "A fresh question about one item")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0].events[-1].kind, "session.opened")
            self.assertEqual(occasions[1]["opportunity_id"], "second")
            self.assertEqual(store.load(FRIEDL).turns[-1].content,
                             "A fresh question about one item")
            store.close()

    def test_no_occasion_means_no_core_turn(self) -> None:
        class Gateway:
            def receive_session_open(self, *arguments):
                raise AssertionError("Core must not be called")

        session = VoiceSession(Gateway(), object(), object(), "friedl", 3, 30,
                               undelivered=lambda: ())
        self.assertIsNone(asyncio.run(session.session_open_response(FRIEDL)))


class LegacyCleanupTests(unittest.TestCase):
    def test_old_reply_text_is_discarded_on_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending-messages.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE pending_messages (text TEXT)")
                connection.execute("INSERT INTO pending_messages VALUES ('stale words')")
            pending = SQLitePendingRelays(path)
            self.assertEqual(pending.relays(NOW), ())
            pending.close()
            with sqlite3.connect(path) as connection:
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'pending_messages'"
                ).fetchone())

    def test_no_competing_replay_entry_point_remains(self) -> None:
        source = ROOT / "src/alx"
        self.assertFalse((source / "continuity/pending_messages.py").exists())
        forbidden = {"_take_waiting", "_keep_unspoken", "_record_waiting",
                     "SQLitePendingMessages", "take_all"}
        for path in source.rglob("*.py"):
            tree = ast.parse(path.read_text())
            names = {node.name for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                          ast.ClassDef))}
            self.assertFalse(names & forbidden, path)


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
    def test_legacy_relays_are_not_backfilled_as_observation_receipts(self) -> None:
        from alx.contracts.mail import MailReference

        self.gateway.relay_response(MAIL, "t1", FRIEDL)
        path = self.store._connection.execute("PRAGMA database_list").fetchone()[2]
        with self.store._connection:
            self.store._connection.execute("PRAGMA user_version = 3")
        self.store.close()
        self.store = SQLiteConversationStore(path)
        self.addCleanup(self.store.close)
        self.assertIsNone(self.store.mail_observation_raised_at(
            MailReference("INBOX", "777", "1")))

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
