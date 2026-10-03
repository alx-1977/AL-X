"""Autonomous cognition on the conversational subscription Core.

EX-001 and D-024a were amended on 2026-09-27: autonomous turns run on the same
`claude_subscription` / `claude-opus-5-5` Core as conversation, because every
metered key is deliberately disabled and the approved Luna arrangement could no
longer run. Nothing woke AL/X for the revisits she had asked for, and six of
them sat overdue.

These tests hold the switch-on safe:

- the only autonomous Core that can be built is the conversational one;
- its zero-rate reservation still marks dispatch, so recovery never replays;
- the tick asks what is due before every turn, so a revisit she withdraws is
  never run after she has withdrawn it;
- she sees her own pending revisits, and the six overdue ones are serviced one
  at a time, each at most once, with none lost unless she withdraws it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.autonomous import AutonomousCognitionRunner  # noqa: E402
from alx.bootstrap.providers import build_runtime_providers  # noqa: E402
from alx.config.settings import RuntimeSettings  # noqa: E402
from alx.continuity import (  # noqa: E402
    DueCognitionSource,
    FutureCognitionSource,
    SQLiteContinuityStore,
    SQLiteOpportunityLedger,
)
from alx.contracts import (  # noqa: E402
    AgentDecision,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CognitionOrigin,
    ReasoningContext,
)
from alx.contracts.continuity import (  # noqa: E402
    FutureCognitionRequest,
    FutureCognitionStatus,
)
from alx.conversation.gateway import ConversationGateway  # noqa: E402
from alx.conversation.store import SQLiteConversationStore  # noqa: E402
from alx.core import CoreAgent  # noqa: E402
from alx.core.model_reasoner import _context_payload  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.observability import ConfiguredPricingWorstCase  # noqa: E402
from alx.observability.autonomous_budget import SQLiteAutonomousLedger  # noqa: E402
from alx.observability.pricing import price_of  # noqa: E402
from alx.providers.claude_subscription import (  # noqa: E402
    ClaudeSubscriptionReasoningModel,
)
from alx.tools.continuity import (  # noqa: E402
    DEFINITIONS as CONTINUITY_DEFINITIONS,
    WITHDRAW_FUTURE_COGNITION,
    build_continuity_executors,
)

SOURCE = Path(__file__).resolve().parents[1] / "src" / "alx"
SUBSCRIPTION = ("claude_subscription", "claude-opus-5-5")
NOW = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)

# The shape of the backlog found on 2026-09-27: six overdue revisits across
# five threads, two sharing one. Identities, times and threads are synthetic;
# nothing here is copied from a live store.
OVERDUE = (
    ("revisit-review-wait-a", "2026-09-07T06:00:00+00:00",
     "00000000-0000-4000-8000-00000000000a"),
    ("revisit-review-wait-b", "2026-09-19T15:00:00+00:00",
     "00000000-0000-4000-8000-00000000000b"),
    ("revisit-review-wait-c1", "2026-09-24T17:30:00+00:00",
     "00000000-0000-4000-8000-00000000000c"),
    ("revisit-review-wait-c2", "2026-09-24T17:45:00+00:00",
     "00000000-0000-4000-8000-00000000000c"),
    ("revisit-review-wait-d", "2026-09-25T20:00:00+00:00",
     "00000000-0000-4000-8000-00000000000d"),
    ("revisit-review-wait-e", "2026-09-27T18:15:00+00:00",
     "00000000-0000-4000-8000-00000000000e"),
)

BASE_ENVIRONMENT = {
    "ALX_REASONING_PROVIDER": "claude_subscription",
    "ALX_CLAUDE_ACCOUNT": "core@example.invalid",
    "ALX_REASONING_MODEL": "claude-opus-5-5",
    "ALX_REASONING_TIMEOUT_SECONDS": "300",
    "ALX_STT_PROVIDER": "cartesia",
    "ALX_STT_MODEL": "ink-whisper",
    "ALX_STT_API_KEY": "stt",
    "ALX_STT_API_VERSION": "2024-11-13",
    "ALX_STT_TURN_START_THRESHOLD": "0.7",
    "ALX_STT_TURN_EAGER_END_THRESHOLD": "0.4",
    "ALX_STT_TURN_END_THRESHOLD": "0.1",
    "ALX_STT_TURN_END_TIMEOUT_MS": "1000",
    "ALX_TTS_PROVIDER": "elevenlabs",
    "ALX_TTS_MODEL": "eleven_v3",
    "ALX_TTS_API_KEY": "tts",
    "ALX_TTS_VOICE_ID": "voice",
    "ALX_TTS_PRONUNCIATION_DICTIONARY_ID": "dictionary",
    "ALX_TTS_PRONUNCIATION_DICTIONARY_VERSION_ID": "version",
}


class TheSubscriptionCoreIsTheAutonomousCoreTests(unittest.TestCase):
    def _providers(self, **overrides):
        settings = RuntimeSettings.from_environment({**BASE_ENVIRONMENT, **overrides})
        with patch("alx.bootstrap.providers.subscription_cli_present", return_value=True):
            return settings, build_runtime_providers(settings)

    def test_switched_on_it_builds_the_conversational_core_again(self) -> None:
        settings, providers = self._providers(
            ALX_AUTONOMOUS_PROVIDER="claude_subscription",
            ALX_AUTONOMOUS_MODEL="claude-opus-5-5",
        )
        self.assertIsInstance(providers.autonomous, ClaudeSubscriptionReasoningModel)
        self.assertIsInstance(providers.reasoning, ClaudeSubscriptionReasoningModel)
        self.assertEqual(providers.autonomous._model, providers.reasoning._model)
        # Its own instance: the bound and the ledger wrap it, not conversation.
        self.assertIsNot(providers.autonomous, providers.reasoning)
        # It waits as long as the Core it is, and holds no key.
        self.assertEqual(settings.autonomous.timeout_seconds, 300)
        self.assertEqual(settings.autonomous.api_key, "")

    def test_switched_off_there_is_no_autonomous_core(self) -> None:
        _settings, providers = self._providers()
        self.assertIsNone(providers.autonomous)

    def test_no_metered_key_is_needed(self) -> None:
        environment = {
            **BASE_ENVIRONMENT,
            "ALX_AUTONOMOUS_PROVIDER": "claude_subscription",
            "ALX_AUTONOMOUS_MODEL": "claude-opus-5-5",
        }
        self.assertFalse(any("OPENAI" in key for key in environment))
        self._providers(**environment)

    def test_the_luna_build_path_is_gone(self) -> None:
        """Law 0: the replaced autonomous construction is deleted, not kept."""
        source = (SOURCE / "bootstrap" / "providers.py").read_text(encoding="utf-8")
        self.assertNotIn("_build_reasoning_model(settings.autonomous", source)


class ZeroRateStillMarksDispatchTests(unittest.TestCase):
    def test_the_subscription_rate_is_recorded_as_zero(self) -> None:
        self.assertEqual(tuple(price_of(*SUBSCRIPTION)), (0.0, 0.0, 0.0, None))

    def test_a_reservation_still_marks_the_turn_dispatched(self) -> None:
        """Recovery reads this mark; without it a crashed turn would replay."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ledger = SQLiteAutonomousLedger(
            Path(directory.name) / "s.sqlite3", 0.5405, ConfiguredPricingWorstCase()
        )
        reservation = ledger.reserve(*SUBSCRIPTION, 96_000, 32_000, "self:r1")
        self.assertEqual(reservation.reserved_usd, 0.0)
        self.assertFalse(ledger.dispatch_started("self:r1"))
        ledger.mark_dispatched(reservation)
        self.assertTrue(ledger.dispatch_started("self:r1"))


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = SQLiteContinuityStore(self.root / "c.sqlite3")
        self.addCleanup(self.store.close)
        self.ledger = SQLiteOpportunityLedger(self.root / "o.sqlite3")
        self.addCleanup(self.ledger.close)

    def request(self, request_id: str, not_before: str, conversation: str) -> None:
        when = datetime.fromisoformat(not_before)
        self.store.create(FutureCognitionRequest(
            request_id, when, f"note for {request_id}", when - timedelta(minutes=6),
            conversation_id=conversation,
        ))

    def source(self) -> FutureCognitionSource:
        return FutureCognitionSource(self.store, self.ledger, enabled=True,
                                     clock=lambda: NOW)

    def tick(self, source, gateway) -> int:
        runner = AutonomousCognitionRunner(source, self.ledger, gateway, 8, 365,
                                           clock=lambda: NOW)
        return asyncio.run(
            DueCognitionSource(source, runner, asyncio.Lock(), 30.0).tick()
        )

    def status(self, request_id: str) -> FutureCognitionStatus:
        rows = {item.request_id: item for item in self.store.pending()}
        if request_id in rows:
            return rows[request_id].status
        return self.store._row_to_request(self.store._connection.execute(
            "SELECT * FROM future_cognition WHERE request_id = ?", (request_id,)
        ).fetchone()).status


class TickAsksBeforeEveryTurnTests(Harness):
    def test_a_revisit_withdrawn_during_the_tick_never_runs(self) -> None:
        for request_id, when, conversation in OVERDUE[2:5]:
            self.request(request_id, when, conversation)
        store = self.store

        class Gateway:
            woken: list[str] = []

            def receive_cognition_opportunity(self, conversation_id, opportunity, *rest):
                self.woken.append(opportunity.opportunity_id)
                if len(self.woken) == 1:
                    # Her judgement in the first turn: the second is superseded.
                    store.withdraw("revisit-review-wait-c2")
                return type("Outcome", (), {
                    "state": type("State", (), {"value": "finished_silently"})(),
                    "response": None,
                })()

        gateway = Gateway()
        self.assertEqual(self.tick(self.source(), gateway), 2)
        self.assertEqual(gateway.woken, [
            "self:revisit-review-wait-c1",
            "self:revisit-review-wait-d",
        ])
        self.assertIs(self.status("revisit-review-wait-c2"),
                      FutureCognitionStatus.WITHDRAWN)
        self.assertFalse(self.ledger.exists("self:revisit-review-wait-c2"))

    def test_an_occasion_declined_without_a_claim_is_not_spun(self) -> None:
        """Offered once per tick: a declined occasion waits for the next tick."""
        self.request(*OVERDUE[0])

        class Declining:
            offered: list[str] = []

            def run_one(self, opportunity):
                self.offered.append(opportunity.opportunity_id)
                return False

        runner = Declining()
        ran = asyncio.run(
            DueCognitionSource(self.source(), runner, asyncio.Lock(), 30.0).tick()
        )
        self.assertEqual(ran, 0)
        self.assertEqual(runner.offered, [f"self:{OVERDUE[0][0]}"])


class PendingRevisitsReachHerTests(Harness):
    def test_the_core_shows_every_turn_her_pending_revisits(self) -> None:
        for item in OVERDUE:
            self.request(*item)
        seen = []

        class Reasoner:
            def decide(self, context):
                seen.append(context.pending_revisits)
                return AgentDecision(response="ok")

        goals = SQLiteGoalStore(self.root / "g.sqlite3")
        self.addCleanup(goals.close)
        core = CoreAgent(goals, Reasoner(), lambda call, state: None, (),
                         clock=lambda: NOW,
                         pending_revisits=lambda: self.store.pending())
        from alx.contracts import ConversationSnapshot

        core.process(ConversationSnapshot("c1", (), 1, NOW + timedelta(days=1)),
                     NOW + timedelta(days=1), 2,
                     origin=CognitionOrigin.SELF_REQUESTED)
        self.assertEqual([item.request_id for item in seen[0]],
                         [item[0] for item in OVERDUE])

    def test_the_payload_carries_her_notes_verbatim(self) -> None:
        self.request(*OVERDUE[5])
        payload = json.loads(_context_payload(ReasoningContext(
            None, (), (), conversation_id="c1",
            pending_revisits=self.store.pending(),
        )))
        self.assertEqual(payload["pending_revisits"], [{
            "request_id": OVERDUE[5][0],
            "not_before": OVERDUE[5][1],
            "note": f"note for {OVERDUE[5][0]}",
            "references": [],
        }])

    def test_composition_shows_them_bounded_on_every_turn(self) -> None:
        source = (SOURCE / "bootstrap" / "live_voice.py").read_text(encoding="utf-8")
        self.assertIn("pending_revisits=lambda: continuity_runtime.store.pending()[", source)
        self.assertIn(":PENDING_REVISIT_LIMIT", source)


class Reasoner:
    """Stands in for her judgement of the six, through real capabilities."""

    def __init__(self, keep: frozenset[str]) -> None:
        self.keep = keep
        self.contexts: list[ReasoningContext] = []

    def decide(self, context):
        self.contexts.append(context)
        withdrawn = {
            attempt.call.arguments["request_id"]
            for attempt in context.transient_attempts
        }
        own = context.events[-1].event_id.removeprefix("self:")
        for item in context.pending_revisits:
            if (item.request_id != own and item.request_id not in self.keep
                    and item.request_id not in withdrawn):
                return AgentDecision(call=CapabilityCall(
                    f"withdraw-{item.request_id}", WITHDRAW_FUTURE_COGNITION,
                    {"request_id": item.request_id},
                ))
        return AgentDecision(finish_silently=True)


class TheSixOverdueRevisitsTests(Harness):
    """Switching on with the live backlog: sequential, deduplicated, none lost."""

    def run_backlog(self, keep: frozenset[str]):
        for item in OVERDUE:
            self.request(*item)
        current = [""]
        continuity = build_continuity_executors(
            self.store, 30, lambda: current[0], clock=lambda: NOW,
            autonomous_available=True,
        )

        def dispatch(call, state):
            # As in production: the executing call names its result.
            current[0] = call.call_id
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                continuity[call.capability_id](call.arguments),
            )

        goals = SQLiteGoalStore(self.root / "g.sqlite3")
        self.addCleanup(goals.close)
        conversations = SQLiteConversationStore(self.root / "conv.sqlite3")
        self.addCleanup(conversations.close)
        reasoner = Reasoner(keep)
        identifiers = iter(f"id-{index}" for index in range(1000))
        core = CoreAgent(goals, reasoner, dispatch, CONTINUITY_DEFINITIONS,
                         clock=lambda: NOW,
                         identifier_factory=lambda: next(identifiers),
                         pending_revisits=lambda: self.store.pending())
        gateway = ConversationGateway(core, conversations,
                                      identifier_factory=lambda: next(identifiers),
                                      clock=lambda: NOW)
        woken: list[str] = []
        receive = gateway.receive_cognition_opportunity

        def counted(conversation_id, opportunity, *rest):
            woken.append(opportunity.opportunity_id)
            return receive(conversation_id, opportunity, *rest)

        gateway.receive_cognition_opportunity = counted  # type: ignore[method-assign]
        ran = self.tick(self.source(), gateway)
        return ran, woken, reasoner

    def test_she_sees_all_six_and_withdraws_what_is_superseded(self) -> None:
        ran, woken, reasoner = self.run_backlog(keep=frozenset())
        # One turn: the oldest, in which she saw the others and closed them.
        self.assertEqual(ran, 1)
        self.assertEqual(woken, [f"self:{OVERDUE[0][0]}"])
        first = reasoner.contexts[0]
        self.assertEqual({item.request_id for item in first.pending_revisits},
                         {item[0] for item in OVERDUE})
        self.assertEqual(self.store.pending(), ())
        # Nothing was deleted: every withdrawn revisit is still inspectable.
        for request_id, _when, _conversation in OVERDUE[1:]:
            self.assertIs(self.status(request_id), FutureCognitionStatus.WITHDRAWN)
        self.assertIs(self.status(OVERDUE[0][0]), FutureCognitionStatus.HONOURED)

    def test_a_revisit_she_keeps_is_serviced_once_in_its_own_turn(self) -> None:
        kept = OVERDUE[5][0]
        ran, woken, _ = self.run_backlog(keep=frozenset({kept}))
        self.assertEqual(ran, 2)
        self.assertEqual(woken, [f"self:{OVERDUE[0][0]}", f"self:{kept}"])
        # In its own thread, and consumed: another tick runs nothing.
        self.assertEqual(self.store.pending(), ())
        self.assertEqual(self.tick(self.source(), _Refusing()), 0)

    def test_switched_off_the_six_wait_untouched(self) -> None:
        for item in OVERDUE:
            self.request(*item)
        disabled = FutureCognitionSource(self.store, self.ledger, enabled=False,
                                         clock=lambda: NOW)
        self.assertEqual(self.tick(disabled, _Refusing()), 0)
        self.assertEqual(len(self.store.pending()), 6)
        self.assertEqual(self.ledger.rows(), ())


class _Refusing:
    def receive_cognition_opportunity(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("nothing should wake")


if __name__ == "__main__":
    unittest.main()
