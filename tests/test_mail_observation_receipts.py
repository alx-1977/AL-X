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
        gateway.response_delivered(source, outcome.response_turn_id)
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
        gateway.response_delivered(source, outcome.response_turn_id)
        gateway._clock = lambda: NOW + timedelta(minutes=10)
        gateway.relay_response(source, outcome.response_turn_id, 'friedl')
        gateway.response_delivered(source, outcome.response_turn_id)
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

    def test_synthesis_without_text_send_cannot_confirm_delivery(self):
        from alx.interfaces.live_voice import StoredResponse, VoiceSession

        gateway, outcome, source = self.respond((A,), person=True)
        session = VoiceSession(gateway, object(), None, 'friedl', 1, 30)

        async def synthesize():
            async for _ in session._speak(source, StoredResponse(outcome.response, outcome.response_turn_id)):
                pass

        asyncio.run(synthesize())
        self.assertIsNone(self.store.mail_observation_raised_at(A))

    def test_terminal_response_excludes_observation_arriving_after_context(self):
        self.events = [event(A)]
        gateway, outcome, source = self.respond((A, B), during=lambda: self.events.append(event(B)))
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.raised_mail_observations, (A,))
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertIsNone(self.store.mail_observation_raised_at(B))
        self.assertFalse(gateway.response_delivered(source, 'missing'))
        self.assertTrue(gateway.response_delivered(source, outcome.response_turn_id))
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)
        self.assertIsNone(self.store.mail_observation_raised_at(B))

    def test_terminal_response_drops_stale_references_and_receipts_only_named_context(self):
        self.events = [event(A)]
        contexts = []

        class Reasoner:
            def decide(inner, context):
                contexts.append(context)
                # Arriving during reasoning does not put B in this turn's context.
                self.events.append(event(B))
                return AgentDecision(response='Core-authored announcement',
                                     raised_mail_observations=(A, B))

        core = CoreAgent(self.goals, Reasoner(), lambda call, state: None, ())
        gateway = ConversationGateway(core, self.store, contextual_events=lambda: tuple(self.events),
                                      clock=lambda: NOW)
        with self.assertLogs('alx.core.loop', level='INFO') as logs:
            outcome = gateway.receive_conversation_turn(ConversationTurn(
                'friedl', 'person', ConversationOrigin.TYPED, 'What is waiting?', NOW), 1, KEEP)
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[-1].response_only_reason, 'decision_rejected')
        self.assertTrue(any('dropped 1 out-of-context mail observations' in log
                            for log in logs.output))
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, 'Core-authored announcement')
        self.assertEqual(outcome.raised_mail_observations, (A,))
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertIsNone(self.store.mail_observation_raised_at(B))
        self.assertTrue(gateway.response_delivered('friedl', outcome.response_turn_id))
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)
        self.assertIsNone(self.store.mail_observation_raised_at(B))

    def test_main_loop_rejects_stale_reference_and_allows_reconsideration(self):
        self.events = [event(A)]
        contexts = []

        class Reasoner:
            def decide(inner, context):
                contexts.append(context)
                return AgentDecision(response='Rejected' if len(contexts) == 1 else 'Reconsidered',
                                     raised_mail_observations=(A, B) if len(contexts) == 1 else ())

        core = CoreAgent(self.goals, Reasoner(), lambda call, state: None, ())
        gateway = ConversationGateway(core, self.store, contextual_events=lambda: tuple(self.events),
                                      clock=lambda: NOW)
        outcome = gateway.receive_conversation_turn(ConversationTurn(
            'friedl', 'person', ConversationOrigin.TYPED, 'What is waiting?', NOW), 2, KEEP)
        self.assertEqual(len(contexts), 2)
        self.assertIsNone(contexts[-1].response_only_reason)
        self.assertIn({'reason': 'decision_rejected',
                       'subject': 'raised_mail_observation_not_in_context'}, contexts[-1].refused_calls)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, 'Reconsidered')
        self.assertEqual(outcome.raised_mail_observations, ())
        self.assertTrue(gateway.response_delivered('friedl', outcome.response_turn_id))
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertIsNone(self.store.mail_observation_raised_at(B))

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
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        self.assertIsNone(self.store.mail_observation_raised_at(B))

    def queued_browser_delivery(self, mode, occasion=True):
        from alx.interfaces.live_voice import VoiceSession
        from alx.interfaces.server import LiveVoiceServer

        gateway, outcome, source = self.respond((A,))

        class QuietTranscriber:
            async def transcribe(self, audio):
                await asyncio.Event().wait()
                yield None

        session = VoiceSession(gateway, QuietTranscriber(), None, 'friedl', 1, 30)
        server = LiveVoiceServer(session, '127.0.0.1', 0, 16000,
                                 Path(self.directory.name), relay=gateway.relay_response,
                                 locate=gateway.locate_reply)

        async def run():
            listener = asyncio.Queue()
            server._delivery_queues['friedl'] = [listener]
            server._live_conversations = ['friedl']
            server._delivery_loop = asyncio.get_running_loop()
            await asyncio.to_thread(server.deliver, source, outcome.response,
                                    'occasion' if occasion else None)
            # Identical later wording must not replace the originating identity.
            current = self.store.load(source)
            self.store.append(ConversationTurn(
                source, 'later', ConversationOrigin.ALX_RESPONSE, outcome.response, NOW,
                raised_mail_observations=(B,)), KEEP, current.revision)
            self.assertIsNone(self.store.mail_observation_raised_at(A))
            if mode == 'close':
                return

            class Connection:
                async def send(inner, frame):
                    import json
                    if json.loads(frame).get('type') == 'alx.text':
                        self.assertIsNone(self.store.mail_observation_raised_at(A))
                        if mode == 'cancel':
                            raise asyncio.CancelledError
                        if mode == 'fail':
                            raise RuntimeError('text send failed')
                    elif json.loads(frame).get('value') == 'listening':
                        raise RuntimeError('end test exchange')

            if mode == 'cancel':
                with self.assertRaises(asyncio.CancelledError):
                    await server._exchange_once(Connection(), 'friedl')
            else:
                self.assertFalse(await server._exchange_once(Connection(), 'friedl'))

        asyncio.run(run())
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW if mode == 'success' else None)
        self.assertIsNone(self.store.mail_observation_raised_at(B))

    def test_queued_connection_close_does_not_raise_mail(self):
        self.queued_browser_delivery('close')

    def test_queued_text_send_failure_does_not_raise_mail(self):
        self.queued_browser_delivery('fail')

    def test_cancelled_queued_text_send_does_not_raise_mail(self):
        self.queued_browser_delivery('cancel')

    def test_queued_text_send_confirms_original_named_context(self):
        self.queued_browser_delivery('success')

    def test_background_response_without_occasion_keeps_identity(self):
        self.queued_browser_delivery('success', occasion=False)

    def test_relay_storage_is_not_delivery_confirmation(self):
        gateway, outcome, source = self.respond((A,))
        self.assertTrue(gateway.relay_response(source, outcome.response_turn_id, 'friedl'))
        self.assertIsNone(self.store.mail_observation_raised_at(A))

    def test_failed_phone_response_keeps_identity_for_browser_fallback(self):
        from types import SimpleNamespace
        from alx.interfaces.phone import PhoneLink
        from alx.contracts.phone import IncomingMessage

        gateway, outcome, source = self.respond((A,), person=True)

        async def remote_turn(*args, **kwargs):
            return outcome

        link = PhoneLink(object(), SimpleNamespace(remote_turn=remote_turn),
                         Path(self.directory.name) / 'cursor', lambda: ('here', None))
        link.send = lambda text: False
        fallback = []
        link.fallback = fallback.append
        asyncio.run(link._answer(IncomingMessage(1, 'hello', NOW)))
        self.assertEqual(getattr(fallback[0], 'turn_id', None), outcome.response_turn_id)
        self.assertEqual(getattr(fallback[0], 'conversation_id', None), 'phone')
        self.assertIsNone(self.store.mail_observation_raised_at(A))

    def test_background_phone_confirmation_requires_successful_send(self):
        from types import SimpleNamespace
        from alx.contracts import ResponseDelivery
        from alx.interfaces.server import LiveVoiceServer

        gateway, outcome, source = self.respond((A,))
        accepted = False

        def send(text):
            self.assertIsNone(self.store.mail_observation_raised_at(A))
            return accepted

        server = LiveVoiceServer(
            SimpleNamespace(response_delivered=gateway.response_delivered),
            '127.0.0.1', 0, 16000, Path(self.directory.name),
            relay=gateway.relay_response, locate=gateway.locate_reply,
            phone=SimpleNamespace(carries=lambda _: True, send=send))
        self.assertIs(server.deliver(source, outcome.response), ResponseDelivery.UNDELIVERABLE)
        self.assertIsNone(self.store.mail_observation_raised_at(A))
        accepted = True
        self.assertIs(server.deliver(source, outcome.response), ResponseDelivery.DELIVERED)
        self.assertEqual(self.store.mail_observation_raised_at(A), NOW)
        self.assertIsNone(self.store.mail_observation_raised_at(B))

    def test_reference_validation_checks_mailbox_and_uid_validity(self):
        for ref in (MailReference('Other', '777', '1'), MailReference('INBOX', '888', '1')):
            with self.subTest(reference=ref):
                gateway, outcome, source = self.respond((ref,))
                self.assertEqual(outcome.state, CoreState.RESPONDED)
                self.assertEqual(outcome.raised_mail_observations, ())
                self.assertTrue(gateway.response_delivered(source, outcome.response_turn_id))
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
            async for _ in session._deliver_response(source, original):
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
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {'relay_response', '_speak'}:
                    calls = {call.func.attr for call in ast.walk(node)
                             if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)}
                    self.assertNotIn('response_delivered', calls, path)
                    self.assertNotIn('record_response_delivery', calls, path)
        store_source = (Path(__file__).resolve().parents[1] / 'src/alx/conversation/store.py').read_text()
        self.assertEqual(store_source.count('response_relays'), 1)
        self.assertIn('DROP TABLE IF EXISTS response_relays', store_source)
