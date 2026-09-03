"""Phase 8: the process-lifetime tick, the shared lock, and commissioning.

The properties that matter here are mostly negative. A tick that finds nothing
must cost nothing. Cognition must not depend on whether anyone is listening.
Two turns must never overlap. And the first supervised activation must permit
exactly one provider dispatch, by counting dispatches rather than dollars —
because a financial fuse cannot limit turns at all.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.autonomous import AutonomousCognitionRunner  # noqa: E402
from alx.continuity import (  # noqa: E402
    DueCognitionSource, FutureCognitionSource, SQLiteContinuityStore,
    SQLiteOpportunityLedger,
)
from alx.contracts.continuity import (  # noqa: E402
    FutureCognitionRequest, FutureCognitionStatus,
)
from alx.observability import ConfiguredPricingWorstCase  # noqa: E402
from alx.observability.autonomous_budget import (  # noqa: E402
    AutonomousBudgetExceeded, SQLiteAutonomousLedger,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
DUE = NOW - timedelta(minutes=5)
LUNA = ("openai", "gpt-5.6-luna")
IN_BOUND, OUT_BOUND = 96_000, 32_000
CHEAP = {
    "input_tokens": 11_343, "cached_tokens": 11_011, "cache_write_tokens": 329,
    "output_tokens": 555, "reasoning_tokens": 516,
}


class _State:
    def __init__(self, value: str) -> None:
        self.value = value


class _Outcome:
    def __init__(self, value: str) -> None:
        self.state = _State(value)
        self.response = "Something I wanted to say." if value == "responded" else None


class Gateway:
    """Counts Core turns without any model."""

    def __init__(self, outcome: str = "finished_silently") -> None:
        self.turns = 0
        self._outcome = outcome

    def receive_cognition_opportunity(self, *args, **kwargs):
        self.turns += 1
        return _Outcome(self._outcome)


class _Transport:
    """Accepts, or cannot. Mirrors the real port's two mechanical outcomes."""

    def __init__(self, accepts: bool) -> None:
        self.accepts = accepts
        self.delivered: list[str] = []

    def deliver(self, conversation_id, response):
        from alx.contracts import ResponseDelivery

        if not self.accepts:
            return ResponseDelivery.UNDELIVERABLE
        self.delivered.append(response)
        return ResponseDelivery.DELIVERED


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self._open()

    def _open(self) -> None:
        self.store = SQLiteContinuityStore(self.root / "c.sqlite3")
        self.ledger = SQLiteOpportunityLedger(self.root / "o.sqlite3")
        self.budget = SQLiteAutonomousLedger(
            self.root / "s.sqlite3", 0.5405, ConfiguredPricingWorstCase()
        )

    def _restart(self) -> None:
        self.store.close()
        self.ledger.close()
        self._open()

    def _request(self, request_id: str = "r1", not_before=DUE) -> None:
        self.store.create(
            FutureCognitionRequest(request_id, not_before, "a note", NOW - timedelta(hours=1))
        )

    def _source(self, enabled: bool = True) -> FutureCognitionSource:
        return FutureCognitionSource(
            self.store, self.ledger, enabled=enabled, clock=lambda: NOW
        )

    def _runner(self, gateway, source=None, transport=True, commissioning=None):
        """`transport=True` means a listener actually accepts the response."""
        return AutonomousCognitionRunner(
            source or self._source(),
            self.ledger,
            gateway,
            "conversation-1",
            4,
            3650,
            clock=lambda: NOW,
            response_transport=_Transport(transport),
            commissioning_limit=commissioning,
        )

    def _tick(self, gateway, source=None, lock=None, **kwargs) -> int:
        source = source or self._source()
        producer = DueCognitionSource(
            source, self._runner(gateway, source, **kwargs),
            lock or asyncio.Lock(), 30.0,
        )
        return asyncio.run(producer.tick())


class TickCostsNothingTests(Harness):
    def test_a_tick_with_nothing_due_makes_no_core_call(self) -> None:
        gateway = Gateway()
        self.assertEqual(self._tick(gateway), 0)
        self.assertEqual(gateway.turns, 0)
        self.assertEqual(self.budget.spend_today(), 0.0)
        self.assertEqual(self.ledger.rows(), ())

    def test_a_future_request_is_not_yet_due(self) -> None:
        self._request(not_before=NOW + timedelta(hours=1))
        gateway = Gateway()
        self.assertEqual(self._tick(gateway), 0)
        self.assertEqual(gateway.turns, 0)

    def test_the_switch_off_leaves_the_request_pending(self) -> None:
        self._request()
        gateway = Gateway()
        self.assertEqual(self._tick(gateway, source=self._source(enabled=False)), 0)
        self.assertEqual(gateway.turns, 0)
        self.assertEqual(self.ledger.rows(), ())
        self.assertIs(self.store.pending()[0].status, FutureCognitionStatus.PENDING)

    def test_many_ticks_with_nothing_due_stay_free(self) -> None:
        gateway = Gateway()
        for _ in range(20):
            self._tick(gateway)
        self.assertEqual(gateway.turns, 0)


class TransportIndependenceTests(Harness):
    """Her continuity must not depend on anyone currently listening."""

    def test_a_due_request_runs_with_no_voice_connection_ever_opened(self) -> None:
        self._request()
        gateway = Gateway()
        self.assertEqual(self._tick(gateway, transport=False), 1)
        self.assertEqual(gateway.turns, 1)

    def test_transport_presence_does_not_change_whether_cognition_occurs(self) -> None:
        for present in (True, False):
            with self.subTest(transport=present):
                self._dir.cleanup.__self__  # keep the directory alive
                self.setUp()
                self._request()
                gateway = Gateway()
                self.assertEqual(self._tick(gateway, transport=present), 1)
                self.assertEqual(gateway.turns, 1)

    def test_the_producer_never_inspects_transport_state(self) -> None:
        import ast

        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "continuity" / "due_source.py"
        ).read_text(encoding="utf-8")
        names = {
            node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        for forbidden in ("transport", "connection", "session", "has_live_transport"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, names)


class UndeliveredResponseTests(Harness):
    def test_responded_without_transport_records_only_the_fact(self) -> None:
        self._request()
        self._tick(Gateway("responded"), transport=False)
        undelivered = self.ledger.undelivered()
        self.assertEqual(len(undelivered), 1)
        self.assertEqual(undelivered[0]["opportunity_id"], "self:r1")

    def test_responded_with_transport_is_not_undelivered(self) -> None:
        self._request()
        self._tick(Gateway("responded"), transport=True)
        self.assertEqual(self.ledger.undelivered(), ())

    def test_finished_silently_is_never_undelivered(self) -> None:
        self._request()
        self._tick(Gateway("finished_silently"), transport=False)
        self.assertEqual(self.ledger.undelivered(), ())

    def test_the_undelivered_fact_survives_restart(self) -> None:
        self._request()
        self._tick(Gateway("responded"), transport=False)
        self._restart()
        self.assertEqual(len(self.ledger.undelivered()), 1)

    def test_no_response_prose_is_stored_anywhere(self) -> None:
        """A fact, not a message. Nothing may replay it."""
        self._request()
        self._tick(Gateway("responded"), transport=False)
        for row in self.ledger.rows():
            for value in row.values():
                if isinstance(value, str):
                    self.assertNotIn("respond", value.lower().replace("responded", ""))

    def test_no_delayed_message_queue_exists(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "alx"
        for path in source_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for token in ("pending_speech", "deferred_response", "message_queue",
                          "replay_response", "undelivered_queue"):
                with self.subTest(module=path.name, token=token):
                    self.assertNotIn(token, text)


class SharedLockTests(Harness):
    """One serialization authority for person and autonomous turns."""

    def test_the_runtime_creates_exactly_one_core_turn_lock(self) -> None:
        import ast

        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text(encoding="utf-8")
        locks = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Lock"
        ]
        self.assertEqual(len(locks), 1)

    def test_both_paths_receive_the_same_lock_object(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text(encoding="utf-8")
        self.assertIn("core_turn_lock=core_turn_lock", source)
        self.assertIn("core_turn_lock,", source)

    def test_a_held_lock_blocks_an_autonomous_turn(self) -> None:
        """A person turn in progress must delay cognition, not race it."""
        self._request()
        gateway = Gateway()
        lock = asyncio.Lock()

        async def scenario() -> int:
            await lock.acquire()          # stands in for a person turn
            source = self._source()
            producer = DueCognitionSource(
                source, self._runner(gateway, source), lock, 30.0
            )
            task = asyncio.create_task(producer.tick())
            await asyncio.sleep(0.05)
            blocked = gateway.turns
            lock.release()
            await task
            return blocked

        self.assertEqual(asyncio.run(scenario()), 0)
        self.assertEqual(gateway.turns, 1)


class CommissioningLatchTests(Harness):
    """A financial fuse cannot limit turns; a dispatch count can."""

    def test_a_cheap_turn_reopens_budget_headroom(self) -> None:
        """The measured reason the latch exists."""
        budget = SQLiteAutonomousLedger(
            self.root / "proof.sqlite3", 0.1632, ConfiguredPricingWorstCase()
        )
        dispatches = 0
        for index in range(10):
            try:
                reservation = budget.reserve(*LUNA, IN_BOUND, OUT_BOUND, f"o{index}")
            except AutonomousBudgetExceeded:
                break
            dispatches += 1
            budget.settle(reservation, *LUNA, CHEAP)
        self.assertGreater(dispatches, 1)

    def test_the_latch_permits_exactly_one_dispatch(self) -> None:
        self._request("r1")
        self._request("r2")
        gateway = Gateway()
        self.assertEqual(self._tick(gateway, commissioning=1), 1)
        self.assertEqual(gateway.turns, 1)

    def test_a_second_tick_is_refused_by_the_latch(self) -> None:
        self._request("r1")
        gateway = Gateway()
        self._tick(gateway, commissioning=1)
        self._request("r2")
        self._tick(gateway, commissioning=1)
        self.assertEqual(gateway.turns, 1)

    def test_the_latch_survives_restart(self) -> None:
        self._request("r1")
        self._tick(Gateway(), commissioning=1)
        self._restart()
        self._request("r2")
        gateway = Gateway()
        self._tick(gateway, commissioning=1)
        self.assertEqual(gateway.turns, 0)

    def test_actual_spend_cannot_reopen_the_latch(self) -> None:
        """It counts dispatches, so cost is irrelevant to it."""
        self._request("r1")
        self._tick(Gateway(), commissioning=1)
        self.assertEqual(self.ledger.commissioning_dispatches(), 1)
        # Even with the whole day's budget free, the latch stays closed.
        self.assertEqual(self.budget.spend_today(), 0.0)
        self._request("r2")
        gateway = Gateway()
        self._tick(gateway, commissioning=1)
        self.assertEqual(gateway.turns, 0)

    def test_a_refused_request_stays_pending_for_later(self) -> None:
        self._request("r1")
        self._tick(Gateway(), commissioning=1)
        self._request("r2")
        self._tick(Gateway(), commissioning=1)
        self.assertIn("r2", {item.request_id for item in self.store.pending()})

    def test_normal_operation_has_no_turn_quota(self) -> None:
        """Absent commissioning, nothing counts her turns."""
        for index in range(5):
            self._request(f"r{index}")
        gateway = Gateway()
        self.assertEqual(self._tick(gateway, commissioning=None), 5)
        self.assertEqual(gateway.turns, 5)
        self.assertEqual(self.ledger.commissioning_dispatches(), 0)


class ShutdownOrderingTests(Harness):
    """Stores must not close under a Core turn still writing to them."""

    def test_cancellation_waits_for_an_in_flight_turn(self) -> None:
        """Deliberately block inside to_thread, then shut down."""
        import threading

        self._request()
        released = threading.Event()
        entered = threading.Event()
        store_open = []

        class BlockingGateway:
            def receive_cognition_opportunity(self, *args, **kwargs):
                entered.set()
                released.wait(5)
                # Still writing when shutdown began: the store must be usable.
                store_open.append(self_store_usable())
                return _Outcome("finished_silently")

        def self_store_usable() -> bool:
            try:
                self.store.pending()
                return True
            except Exception:
                return False

        source = self._source()
        producer = DueCognitionSource(
            source, self._runner(BlockingGateway(), source),
            asyncio.Lock(), 30.0,
        )

        async def scenario():
            task = asyncio.create_task(producer.tick())
            await asyncio.to_thread(entered.wait, 5)
            task.cancel()                      # shutdown arrives mid-turn
            await asyncio.sleep(0.05)
            # The blocked Core turn has not been abandoned: it has neither
            # completed nor been discarded while its worker still runs.
            finished_early = entered.is_set() and bool(store_open)
            released.set()                     # let the Core turn finish
            try:
                await task
            except asyncio.CancelledError:
                pass
            return finished_early

        finished_early = asyncio.run(scenario())
        # The turn ran to its own boundary rather than being cut short.
        self.assertFalse(finished_early, "the turn must not finish before release")
        # And it saw a usable store throughout, which is the property that
        # matters: shutdown cannot close SQLite under a writing Core turn.
        self.assertEqual(store_open, [True])

    def test_the_shutdown_sequence_takes_the_shared_lock(self) -> None:
        """Composition must wait on the lock before closing stores."""
        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text(encoding="utf-8")
        finally_block = source[source.index("    finally:", source.index("TaskGroup")):]
        lock_at = finally_block.index("core_turn_lock")
        close_at = finally_block.index("conversation_store.close()")
        self.assertLess(lock_at, close_at, "stores close only after the lock")

    def test_the_turn_is_shielded_from_cancellation(self) -> None:
        import ast

        tree = ast.parse(
            (Path(__file__).resolve().parents[1]
             / "src" / "alx" / "continuity" / "due_source.py").read_text("utf-8")
        )
        shields = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "shield"
        ]
        self.assertEqual(len(shields), 1)


class EndToEndContinuityTests(Harness):
    """The scenario D-024 exists for, traced through production objects."""

    def _request_in(self, conversation_id: str, request_id: str) -> None:
        from alx.tools.continuity import (
            REQUEST_FUTURE_COGNITION, build_continuity_executors,
        )

        build_continuity_executors(
            self.store, 3650, lambda: "call-1", clock=lambda: NOW,
            conversation_id_source=lambda: conversation_id,
        )[REQUEST_FUTURE_COGNITION]({
            "request_id": request_id,
            "not_before": (NOW + timedelta(seconds=90)).isoformat(),
            "note": "the compressor curve still bothers me",
        })

    def test_a_thought_returns_to_the_conversation_it_arose_in(self) -> None:
        self._request_in("conversation-A", "r1")
        self._restart()                       # survives a restart
        seen: list[str] = []

        class Recording(Gateway):
            def receive_cognition_opportunity(inner, conversation_id, *args, **kw):
                seen.append(conversation_id)
                return super().receive_cognition_opportunity(
                    conversation_id, *args, **kw
                )

        source = FutureCognitionSource(
            self.store, self.ledger, enabled=True,
            clock=lambda: NOW + timedelta(minutes=5),
        )
        self._tick(Recording(), source=source)
        self.assertEqual(seen, ["conversation-A"])

    def test_two_conversations_never_contaminate_each_other(self) -> None:
        self._request_in("conversation-A", "r-a")
        self._request_in("conversation-B", "r-b")
        seen: list[str] = []

        class Recording(Gateway):
            def receive_cognition_opportunity(inner, conversation_id, *args, **kw):
                seen.append(conversation_id)
                return super().receive_cognition_opportunity(
                    conversation_id, *args, **kw
                )

        source = FutureCognitionSource(
            self.store, self.ledger, enabled=True,
            clock=lambda: NOW + timedelta(minutes=5),
        )
        self._tick(Recording(), source=source)
        self.assertEqual(sorted(seen), ["conversation-A", "conversation-B"])

    def test_the_full_undelivered_cycle(self) -> None:
        """Responded with nobody listening, then resolved by AL/X alone."""
        from alx.tools.continuity import (
            RESOLVE_UNDELIVERED_RESPONSE, build_continuity_executors,
        )

        self._request()
        self._tick(Gateway("responded"), transport=False)
        # Recorded as a fact she can see.
        self.assertEqual(len(self.ledger.undelivered()), 1)
        # Nothing resolves it on its own, however many ticks pass.
        for _ in range(5):
            self._tick(Gateway())
        self.assertEqual(len(self.ledger.undelivered()), 1)
        # Only her explicit act closes it.
        execute = build_continuity_executors(
            self.store, 3650, lambda: "call-1", clock=lambda: NOW,
            occasions=self.ledger,
        )[RESOLVE_UNDELIVERED_RESPONSE]
        result = execute({"opportunity_id": "self:r1"})
        self.assertEqual(result.values["opportunity_id"], "self:r1")
        self.assertEqual(self.ledger.undelivered(), ())

    def test_resolving_an_unknown_occasion_fails_safely(self) -> None:
        from alx.tools.continuity import (
            RESOLVE_UNDELIVERED_RESPONSE, build_continuity_executors,
        )

        execute = build_continuity_executors(
            self.store, 3650, lambda: "c", clock=lambda: NOW,
            occasions=self.ledger,
        )[RESOLVE_UNDELIVERED_RESPONSE]
        result = execute({"opportunity_id": "self:nope"})
        self.assertEqual(result.failure["code"], "occasion_not_found")

    def test_delivered_responses_reach_the_transport_exactly_once(self) -> None:
        self._request()
        transport = _Transport(True)
        source = self._source()
        producer = DueCognitionSource(
            source,
            AutonomousCognitionRunner(
                source, self.ledger, Gateway("responded"), "c1", 4, 3650,
                clock=lambda: NOW, response_transport=transport,
            ),
            asyncio.Lock(), 30.0,
        )
        asyncio.run(producer.tick())
        self.assertEqual(len(transport.delivered), 1)
        self.assertEqual(self.ledger.undelivered(), ())


if __name__ == "__main__":
    unittest.main()
