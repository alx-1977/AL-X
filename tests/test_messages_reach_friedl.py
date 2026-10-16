"""D-053: an unheard intention gets one fresh session-open Core turn."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.continuity.ledger import SQLiteOpportunityLedger  # noqa: E402
from alx.contracts import (  # noqa: E402
    CognitionOrigin, ConversationOrigin, ConversationTurn, ResponseDelivery,
)
from alx.conversation.gateway import ConversationGateway  # noqa: E402
from alx.conversation.store import SQLiteConversationStore  # noqa: E402
from alx.core import CoreOutcome, CoreState  # noqa: E402
from alx.interfaces.live_voice import VoiceSession  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402

NOW = datetime(2026, 10, 16, 8, 30, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
MAIL = "mail-thread:<jlcpcb-123@mail>"
FRIEDL = "6f528a72-0933-442c-be31-25778141a08a"
STORED_PROSE = "VERBATIM SECRET RESPONSE THAT MUST NEVER RETURN"


class _Lifecycle:
    def __init__(self, rows):
        self.rows = tuple(rows)
        self.claims = 0
        self.resolved = []
        self.restored = []

    def claim_undelivered_responses(self):
        self.claims += 1
        rows, self.rows = self.rows, ()
        return rows

    def resolve_claimed_responses(self, identities):
        self.resolved.append(tuple(identities))

    def restore_claimed_responses(self, identities):
        self.restored.append(tuple(identities))


class _Core:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome or CoreOutcome(CoreState.RESPONDED, response="Fresh answer.")
        self.error = error
        self.calls = []

    def process(self, conversation, retention, budget, **values):
        self.calls.append((conversation, retention, budget, values))
        if self.error is not None:
            raise self.error
        return self.outcome


def _row(identity, origin="external_event", refs="mail:1\x1fmail:2",
         conversation_id=FRIEDL, response_turn_id=None):
    return {
        "opportunity_id": identity,
        "origin": origin,
        "arose_at": NOW.isoformat(),
        "refs": refs,
        "conversation_id": conversation_id,
        "response_turn_id": response_turn_id or identity,
        "response": STORED_PROSE,
        "text": STORED_PROSE,
        "unapproved_nested": {"wording": STORED_PROSE},
    }


class SessionOpenGatewayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteConversationStore(Path(directory.name) / "conversations.sqlite3")
        self.addCleanup(self.store.close)

    def gateway(self, core, lifecycle):
        return ConversationGateway(
            core, self.store, identifier_factory=lambda: "opening-1",
            clock=lambda: NOW, undelivered_responses=lifecycle,
        )

    def test_many_intentions_make_one_fresh_turn_with_allowlisted_evidence_only(self):
        lifecycle = _Lifecycle((_row("occasion-1"), _row("occasion-2", refs="mail:3")))
        core = _Core()
        gateway = self.gateway(core, lifecycle)

        outcome = gateway.receive_session_open(FRIEDL, 8, RETENTION)

        self.assertEqual(outcome.response, "Fresh answer.")
        self.assertEqual(len(core.calls), 1)
        evidence = core.calls[0][3]["undelivered_responses"]
        self.assertEqual(
            evidence,
            (
                {"opportunity_id": "occasion-1", "origin": "external_event",
                 "arose_at": NOW.isoformat(), "references": ("mail:1", "mail:2")},
                {"opportunity_id": "occasion-2", "origin": "external_event",
                 "arose_at": NOW.isoformat(), "references": ("mail:3",)},
            ),
        )
        self.assertNotIn(STORED_PROSE, repr(core.calls))
        self.assertEqual(lifecycle.resolved, [("occasion-1", "occasion-2")])
        self.assertEqual(lifecycle.restored, [])

    def test_no_intention_makes_no_core_turn(self):
        lifecycle = _Lifecycle(())
        core = _Core()
        self.assertIsNone(self.gateway(core, lifecycle).receive_session_open(FRIEDL, 8, RETENTION))
        self.assertEqual(core.calls, [])

    def test_an_autonomous_reply_uses_its_content_free_opportunity_identity(self):
        core = _Core()
        opportunity = SimpleNamespace(
            opportunity_id="occasion-1",
            origin=CognitionOrigin.SELF_REQUESTED,
            arose_at=NOW,
            note=None,
            provenance=None,
            references=(),
        )

        self.gateway(core, _Lifecycle(())).receive_cognition_opportunity(
            FRIEDL, opportunity, 8, RETENTION
        )

        stored = self.store.load(FRIEDL)
        self.assertEqual(stored.turns[-1].turn_id, "occasion-1")
        self.assertEqual(stored.turns[-1].content, "Fresh answer.")

    def test_same_conversation_omits_only_the_claimed_unheard_reply(self):
        conversation = self.store.create(FRIEDL, RETENTION)
        heard_person = ConversationTurn(
            FRIEDL, "heard-person", ConversationOrigin.TYPED,
            "Ordinary heard question.", NOW, "friedl",
        )
        conversation = self.store.append(
            heard_person, RETENTION, conversation.revision
        )
        heard_reply = ConversationTurn(
            FRIEDL, "heard-reply", ConversationOrigin.ALX_RESPONSE,
            "Ordinary heard answer.", NOW,
        )
        conversation = self.store.append(
            heard_reply, RETENTION, conversation.revision
        )
        unheard_reply = ConversationTurn(
            FRIEDL, "occasion-1", ConversationOrigin.ALX_RESPONSE,
            STORED_PROSE, NOW,
        )
        self.store.append(unheard_reply, RETENTION, conversation.revision)
        lifecycle = _Lifecycle((_row("occasion-1"),))
        core = _Core()

        self.gateway(core, lifecycle).receive_session_open(FRIEDL, 8, RETENTION)

        opening = core.calls[0][0]
        self.assertEqual(
            tuple(turn.turn_id for turn in opening.turns),
            ("heard-person", "heard-reply"),
        )
        self.assertIn("Ordinary heard question.", repr(opening.turns))
        self.assertIn("Ordinary heard answer.", repr(opening.turns))
        self.assertNotIn(STORED_PROSE, repr(opening.turns))
        self.assertEqual(
            core.calls[0][3]["undelivered_responses"][0]["opportunity_id"],
            "occasion-1",
        )

    def test_a_checkpoint_restores_the_claim(self):
        lifecycle = _Lifecycle((_row("occasion-1"),))
        core = _Core(CoreOutcome(CoreState.CHECKPOINTED, reason="budget_exceeded"))
        self.gateway(core, lifecycle).receive_session_open(FRIEDL, 8, RETENTION)
        self.assertEqual(lifecycle.resolved, [])
        self.assertEqual(lifecycle.restored, [("occasion-1",)])

    def test_a_failed_turn_restores_the_claim(self):
        lifecycle = _Lifecycle((_row("occasion-1"),))
        gateway = self.gateway(_Core(error=RuntimeError("provider failed")), lifecycle)
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            gateway.receive_session_open(FRIEDL, 8, RETENTION)
        self.assertEqual(lifecycle.resolved, [])
        self.assertEqual(lifecycle.restored, [("occasion-1",)])

    def test_authoritative_silence_resolves_the_claim(self):
        lifecycle = _Lifecycle((_row("occasion-1"),))
        core = _Core(CoreOutcome(CoreState.FINISHED_SILENTLY))
        self.gateway(core, lifecycle).receive_session_open(FRIEDL, 8, RETENTION)
        self.assertEqual(lifecycle.resolved, [("occasion-1",)])


class LedgerLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = SQLiteOpportunityLedger(Path(directory.name) / "opportunities.sqlite3")
        self.addCleanup(self.ledger.close)
        opportunity = SimpleNamespace(
            opportunity_id="occasion-1", origin=CognitionOrigin.EXTERNAL_EVENT,
            arose_at=NOW, references=("mail:1",), conversation_id=FRIEDL,
        )
        self.ledger.record_created(opportunity)
        self.ledger.record_outcome("occasion-1", "responded")
        self.ledger.mark_response_undelivered("occasion-1", FRIEDL, "occasion-1")

    def test_claim_is_exclusive_and_failure_can_restore_it(self):
        claimed = self.ledger.claim_undelivered_responses()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["conversation_id"], FRIEDL)
        self.assertEqual(claimed[0]["response_turn_id"], "occasion-1")
        self.assertEqual(self.ledger.claim_undelivered_responses(), ())
        self.ledger.restore_claimed_responses(("occasion-1",))
        self.assertEqual(len(self.ledger.claim_undelivered_responses()), 1)

    def test_success_consumes_the_intention(self):
        self.ledger.claim_undelivered_responses()
        self.ledger.resolve_claimed_responses(("occasion-1",))
        self.assertEqual(self.ledger.undelivered(), ())

    def test_restart_restores_a_claim_left_by_a_stopped_turn(self):
        self.ledger.claim_undelivered_responses()
        path = Path(self.ledger._connection.execute("PRAGMA database_list").fetchone()[2])
        self.ledger.close()
        recovered = SQLiteOpportunityLedger(path)
        self.addCleanup(recovered.close)
        self.assertEqual(len(recovered.claim_undelivered_responses()), 1)


class FreshDeliveryTests(unittest.TestCase):
    class Session:
        def admits_unprompted_speech(self, _conversation_id):
            return True

    def server(self, phone=None):
        return LiveVoiceServer(self.Session(), "127.0.0.1", 0, 16000, ROOT, phone=phone)

    def test_fresh_response_reaches_an_already_open_session(self):
        async def scenario():
            server = self.server()
            listener = asyncio.Queue()
            server._delivery_queues[FRIEDL] = [listener]
            server._live_conversations.append(FRIEDL)
            server._delivery_loop = asyncio.get_running_loop()
            result = await asyncio.to_thread(server.deliver, MAIL, "Fresh answer.")
            return result, listener.get_nowait()

        self.assertEqual(asyncio.run(scenario()), (ResponseDelivery.DELIVERED, "Fresh answer."))

    def test_with_no_session_the_words_are_not_queued(self):
        self.assertIs(self.server().deliver(MAIL, STORED_PROSE), ResponseDelivery.UNDELIVERABLE)


class VoiceSessionOpenTests(unittest.TestCase):
    def test_session_open_uses_the_gateway_once(self):
        calls = []
        gateway = SimpleNamespace(
            receive_session_open=lambda *arguments: calls.append(arguments) or None
        )
        session = VoiceSession(gateway, object(), None, "friedl", 8, 30, clock=lambda: NOW)
        self.assertIsNone(asyncio.run(session.open_session(FRIEDL)))
        self.assertEqual(len(calls), 1)


class SessionOpenFailureRecoveryTests(unittest.TestCase):
    class Connection:
        def __init__(self):
            self.request = SimpleNamespace(
                path=f"/voice?conversation_id={FRIEDL}"
            )
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

        async def close(self, **_values):
            pass

    class Session:
        def __init__(self, gateway):
            self.gateway = gateway
            self.exchanges = 0
            self.delivery_sizes = []

        async def open_session(self, conversation_id):
            return self.gateway.receive_session_open(
                conversation_id, 8, RETENTION
            )

        async def exchange(
            self, _conversation_id, _audio, deliveries=None, _typed=None
        ):
            self.exchanges += 1
            self.delivery_sizes.append(deliveries.qsize())
            if False:  # pragma: no cover - makes this an empty async generator
                yield None

    def run_outcome(self, state, reason):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SQLiteConversationStore(
            Path(directory.name) / "conversations.sqlite3"
        )
        self.addCleanup(store.close)
        lifecycle = _Lifecycle((_row("occasion-1"),))
        gateway = ConversationGateway(
            _Core(CoreOutcome(
                state, response="must not be queued", reason=reason
            )),
            store,
            identifier_factory=lambda: "opening-1",
            clock=lambda: NOW,
            undelivered_responses=lifecycle,
        )
        session = self.Session(gateway)
        server = LiveVoiceServer(
            session, "127.0.0.1", 0, 16000, ROOT
        )
        connection = self.Connection()

        asyncio.run(server._handle_voice(connection))

        messages = [json.loads(item) for item in connection.sent]
        return lifecycle, session, messages

    def test_error_uses_existing_diagnostic_restores_claim_and_keeps_session(self):
        lifecycle, session, messages = self.run_outcome(
            CoreState.ERROR, "reasoner_error"
        )

        self.assertIn(
            {
                "type": "diagnostic",
                "code": "session.open_continuity_failed",
                "state": "error",
                "reason": "reasoner_error",
            },
            messages,
        )
        self.assertEqual(lifecycle.restored, [("occasion-1",)])
        self.assertEqual(session.exchanges, 1)
        self.assertEqual(session.delivery_sizes, [0])
        self.assertFalse(any(item["type"] == "alx.text" for item in messages))

    def test_checkpoint_uses_existing_diagnostic_restores_claim_and_keeps_session(self):
        lifecycle, session, messages = self.run_outcome(
            CoreState.CHECKPOINTED, "budget_exceeded"
        )

        self.assertIn(
            {
                "type": "diagnostic",
                "code": "session.open_continuity_failed",
                "state": "checkpointed",
                "reason": "budget_exceeded",
            },
            messages,
        )
        self.assertEqual(lifecycle.restored, [("occasion-1",)])
        self.assertEqual(session.exchanges, 1)
        self.assertEqual(session.delivery_sizes, [0])
        self.assertFalse(any(item["type"] == "alx.text" for item in messages))


class OneProductionPathTests(unittest.TestCase):
    def test_the_verbatim_replay_path_is_absent(self):
        self.assertFalse((ROOT / "src/alx/continuity/pending_messages.py").exists())
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "src/alx").rglob("*.py")
        )
        for fragment in (
            "_take_waiting", "pending_messages", "relay_response", "_retry_relays",
            "ResponseDelivery.QUEUED", "resolve_undelivered_response",
        ):
            self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
