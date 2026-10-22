"""Receipts cover Core-named observations, never a thread or a time window."""
import ast
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from alx.contracts import AgentDecision, BackgroundEvent, ConversationOrigin, ConversationTurn
from alx.contracts.mail import MailReference
from alx.conversation import ConversationGateway, SQLiteConversationStore
from alx.core import CoreAgent, CoreState
from alx.goals import SQLiteGoalStore

NOW = datetime(2026, 10, 22, tzinfo=UTC)
KEEP = NOW + timedelta(days=30)
A = MailReference('INBOX', '777', '1')
B = MailReference('INBOX', '777', '2')


def event(ref, thread=None):
    return BackgroundEvent(f'mail:{ref.uid_validity}:{ref.uid}', 'mail.message_waiting', NOW,
                           {'mailbox_id': ref.mailbox_id, 'uid_validity': ref.uid_validity,
                            'uid': ref.uid, 'message_id': thread or f'<thread-{ref.uid}>'})


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'conversation.sqlite3'
        self.store = SQLiteConversationStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.goals = SQLiteGoalStore(Path(self.directory.name) / 'goals.sqlite3')
        self.addCleanup(self.goals.close)
        self.events = [event(A), event(B)]

    def respond(self, refs, person=False, during=None, memory_conflict=False):
        class Reasoner:
            def decide(inner, context):
                if during:
                    during()
                return AgentDecision(response='Core-authored announcement', raised_mail_observations=refs)
        core = CoreAgent(self.goals, Reasoner(), lambda call, state: None, ())
        if memory_conflict:
            core._conflicting_memories = lambda *args: ({'memory_id': 'clash'},)
        gateway = ConversationGateway(core, self.store, contextual_events=lambda: tuple(self.events),
                                      clock=lambda: NOW)
        if person:
            outcome = gateway.receive_conversation_turn(ConversationTurn(
                'friedl', 'person', ConversationOrigin.TYPED, 'What is waiting?', NOW), 1, KEEP)
        else:
            outcome = gateway.receive_session_open('mail-thread:A', 1, KEEP)
        return gateway, outcome, 'friedl' if person else 'mail-thread:A'

    def test_new_same_thread_observation_between_context_and_delivery_is_not_raised(self):
        self.events = [event(A)]
        gateway, outcome, source = self.respond(
            (A,), during=lambda: self.events.append(event(B, '<thread-1>')))
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        gateway.relay_response(source, outcome.response_turn_id, 'friedl')
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)
        self.assertIsNone(self.store.mail_observation_raised_at(B))
        # Context continues showing the unseen arrival without inheriting a
        # receipt from its thread's earlier, now delivered response.
        context = gateway._with_contextual_events(self.store.load(source))
        self.assertEqual([item.data['already_raised_with_friedl'] for item in context.events],
                         [True, False])

    def test_shared_announcement_marks_both_observations_after_delivery_and_restart(self):
        gateway, outcome, source = self.respond((A, B))
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertIsNone(self.store.mail_observation_raised_at(B))
        gateway.relay_response(source, outcome.response_turn_id, 'friedl')
        gateway._clock = lambda: NOW + timedelta(minutes=10)
        gateway.relay_response(source, outcome.response_turn_id, 'friedl')
        self.store.close()
        self.store = SQLiteConversationStore(self.path)
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)
        self.assertEqual(self.store.mail_observation_raised_at(B), NOW)
        with self.store._connection:
            self.store._connection.execute('DELETE FROM conversation_turns')
        self.assertEqual(self.store.mail_observation_raised_at(B), NOW)
        for other in (MailReference('Other', '777', '1'), MailReference('INBOX', '888', '1')):
            self.assertIsNone(self.store.mail_observation_raised_at(other))

    def test_person_response_records_only_named_observations_on_confirmation(self):
        gateway, outcome, source = self.respond((B,), person=True)
        self.assertIsNone(self.store.mail_observation_raised_at(B))
        gateway.response_delivered(source, outcome.response_turn_id)
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertEqual(self.store.mail_observation_raised_at(B), NOW)

    def test_voice_confirmation_follows_successful_text_delivery(self):
        from alx.interfaces.live_voice import VoiceSession

        gateway, outcome, source = self.respond((A,), person=True)
        session = VoiceSession(gateway, object(), None, 'friedl', 1, 30)

        async def deliver(accept):
            stream = session._response_events(source, outcome)
            await anext(stream)  # text has been offered, not accepted yet
            self.assertIsNone(self.store.mail_observation_raised_at(A))
            if accept:
                async for _ in stream:
                    pass
            await stream.aclose()

        asyncio.run(deliver(False))
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        asyncio.run(deliver(True))
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)

    def test_unknown_and_new_out_of_context_references_reject_entire_response(self):
        self.events = [event(A)]
        gateway, outcome, source = self.respond((A, B), during=lambda: self.events.append(event(B)))
        self.assertNotEqual(outcome.state, CoreState.RESPONDED)
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertIsNone(self.store.mail_observation_raised_at(B))
        self.assertFalse(gateway.response_delivered(source, 'missing'))

    def test_omitted_references_and_legacy_relay_do_not_prove_coverage(self):
        gateway, outcome, source = self.respond(())
        gateway.relay_response(source, outcome.response_turn_id, 'friedl')
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute('CREATE TABLE response_relays (source_conversation_id TEXT, source_turn_id TEXT, raised_at TEXT)')
            connection.execute('INSERT INTO response_relays VALUES (?, ?, ?)', (source, outcome.response_turn_id, NOW.isoformat()))
            connection.execute('PRAGMA user_version = 4')
        self.store = SQLiteConversationStore(self.path)
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertIsNone(self.store._connection.execute("SELECT name FROM sqlite_master WHERE name='response_relays'").fetchone())

    def test_missing_delivery_never_marks_a_named_observation(self):
        gateway, outcome, source = self.respond((A,))
        self.assertFalse(gateway.relay_response(source, 'unknown-response', 'friedl'))
        self.assertIsNone(self.store.mail_observation_raised_at(A))

    def test_transport_refusal_and_acceptance_control_receipts(self):
        from types import SimpleNamespace
        from alx.contracts import ResponseDelivery
        from alx.interfaces.server import LiveVoiceServer

        gateway, outcome, source = self.respond((A, B))
        server = LiveVoiceServer(
            SimpleNamespace(admits_unprompted_speech=lambda _: True),
            '127.0.0.1', 0, 16000, Path(self.directory.name),
            relay=gateway.relay_response, locate=gateway.locate_reply)
        self.assertIs(server.deliver(source, outcome.response), ResponseDelivery.UNDELIVERABLE)
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertIsNone(self.store.mail_observation_raised_at(B))

        async def accept():
            listener = asyncio.Queue()
            server._delivery_queues['friedl'] = [listener]
            server._live_conversations = ['friedl']
            server._delivery_loop = asyncio.get_running_loop()
            result = await asyncio.to_thread(server.deliver, source, outcome.response)
            self.assertIs(result, ResponseDelivery.DELIVERED)
            self.assertEqual(listener.get_nowait(), outcome.response)

        asyncio.run(accept())
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)
        self.assertEqual(self.store.mail_observation_raised_at(B), NOW)

    def test_reference_validation_checks_mailbox_and_uid_validity(self):
        for ref in (MailReference('Other', '777', '1'), MailReference('INBOX', '888', '1')):
            with self.subTest(reference=ref):
                _, outcome, _ = self.respond((ref,))
                self.assertNotEqual(outcome.state, CoreState.RESPONDED)
                self.assertIsNone(self.store.mail_observation_raised_at(ref))

    def test_response_retained_after_memory_conflict_keeps_observation_coverage(self):
        gateway, outcome, source = self.respond((A,), memory_conflict=True)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.memory_state, 'unresolved_identity_conflict')
        gateway.response_delivered(source, outcome.response_turn_id)
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)

    def test_person_confirmation_retains_original_context_with_identical_later_words(self):
        from alx.interfaces.live_voice import StoredResponse, VoiceSession

        gateway, first, source = self.respond((A,), person=True)
        original = StoredResponse(first.response, first.response_turn_id)
        # Another response with exactly the same words covers a different item.
        current = self.store.load(source)
        self.store.append(ConversationTurn(
            source, 'later', ConversationOrigin.ALX_RESPONSE, first.response, NOW,
            raised_mail_observations=(B,)), KEEP, current.revision)
        session = VoiceSession(gateway, object(), None, 'friedl', 1, 30)

        async def accept():
            async for _ in session._speak(source, original):
                pass

        asyncio.run(accept())
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)
        self.assertIsNone(self.store.mail_observation_raised_at(B))

    def test_no_competing_inferred_receipt_path_remains(self):
        forbidden = {'response_relayed_at', '_record_relay'}
        for path in (Path(__file__).resolve().parents[1] / 'src/alx').rglob('*.py'):
            tree = ast.parse(path.read_text())
            names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.assertFalse(names & forbidden, path)
        store_source = (Path(__file__).resolve().parents[1] / 'src/alx/conversation/store.py').read_text()
        self.assertEqual(store_source.count('response_relays'), 1)
        self.assertIn('DROP TABLE IF EXISTS response_relays', store_source)
