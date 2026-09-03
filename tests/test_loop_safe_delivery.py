"""Delivery is confirmed by the loop, and unknown provenance refuses.

Two findings from review, both of the same shape as the original delivery bug:
a path that reports success while her words are lost, and a fallback that sends
a thought to a conversation it never belonged to. Both are asserted here by
behaviour rather than by reading the source.
"""

from __future__ import annotations

import asyncio
import queue as queue_module
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.autonomous import AutonomousCognitionRunner  # noqa: E402
from alx.continuity import (  # noqa: E402
    FutureCognitionSource, SQLiteContinuityStore, SQLiteOpportunityLedger,
)
from alx.contracts import ResponseDelivery  # noqa: E402
from alx.contracts.continuity import FutureCognitionRequest  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
DUE = NOW - timedelta(minutes=5)


def _server() -> LiveVoiceServer:
    server = LiveVoiceServer.__new__(LiveVoiceServer)
    server._delivery_queues = {}
    server._typed_queues = {}
    server._delivery_loop = None
    return server


class LoopSafeDeliveryTests(unittest.TestCase):
    """The enqueue happens on the loop, and DELIVERED means it happened."""

    def _deliver_from_worker(self, prepare) -> dict:
        """Run deliver() on a worker thread while a real loop owns the queue."""
        observed: dict = {}

        async def scenario() -> None:
            server = _server()
            listener: asyncio.Queue[str] = asyncio.Queue()
            server._delivery_queues["c1"] = [listener]
            server._delivery_loop = asyncio.get_running_loop()
            prepare(server, listener)

            loop_thread = threading.get_ident()
            observed["loop_thread"] = loop_thread

            def worker() -> ResponseDelivery:
                observed["worker_thread"] = threading.get_ident()
                return server.deliver("c1", "her words")

            observed["result"] = await asyncio.to_thread(worker)
            observed["queued"] = []
            while not listener.empty():
                observed["queued"].append(listener.get_nowait())

        asyncio.run(scenario())
        return observed

    def test_a_live_listener_receives_the_response_exactly_once(self) -> None:
        observed = self._deliver_from_worker(lambda server, listener: None)
        self.assertIs(observed["result"], ResponseDelivery.DELIVERED)
        self.assertEqual(observed["queued"], ["her words"])

    def test_delivery_runs_on_a_worker_thread_not_the_loop(self) -> None:
        """Proves the scenario exercises the cross-thread case at all."""
        observed = self._deliver_from_worker(lambda server, listener: None)
        self.assertNotEqual(observed["worker_thread"], observed["loop_thread"])

    def test_the_enqueue_itself_happens_on_the_loop_thread(self) -> None:
        seen: dict = {}

        class RecordingQueue(asyncio.Queue):
            def put_nowait(self, item):
                seen["enqueue_thread"] = threading.get_ident()
                super().put_nowait(item)

        async def scenario() -> None:
            server = _server()
            listener = RecordingQueue()
            server._delivery_queues["c1"] = [listener]
            server._delivery_loop = asyncio.get_running_loop()
            seen["loop_thread"] = threading.get_ident()
            await asyncio.to_thread(server.deliver, "c1", "her words")

        asyncio.run(scenario())
        self.assertEqual(seen["enqueue_thread"], seen["loop_thread"])

    def test_no_listener_is_undeliverable(self) -> None:
        server = _server()
        self.assertIs(
            server.deliver("c1", "her words"), ResponseDelivery.UNDELIVERABLE
        )

    def test_a_listener_that_detaches_before_the_enqueue_is_undeliverable(self) -> None:
        def detach(server, listener):
            # Registered at lookup, gone by the time the loop runs the callback.
            original = server.deliver

            def racing(conversation_id, response):
                server._delivery_queues["c1"] = []
                return original(conversation_id, response)

            server.deliver = racing

        observed = self._deliver_from_worker(detach)
        self.assertIs(observed["result"], ResponseDelivery.UNDELIVERABLE)

    def test_an_enqueue_failure_is_undeliverable(self) -> None:
        class RefusingQueue(asyncio.Queue):
            def put_nowait(self, item):
                raise RuntimeError("queue refused")

        async def scenario() -> ResponseDelivery:
            server = _server()
            server._delivery_queues["c1"] = [RefusingQueue()]
            server._delivery_loop = asyncio.get_running_loop()
            return await asyncio.to_thread(server.deliver, "c1", "her words")

        self.assertIs(asyncio.run(scenario()), ResponseDelivery.UNDELIVERABLE)

    def test_a_closed_loop_is_undeliverable(self) -> None:
        server = _server()
        loop = asyncio.new_event_loop()
        loop.close()
        server._delivery_queues["c1"] = [object()]
        server._delivery_loop = loop
        self.assertIs(
            server.deliver("c1", "her words"), ResponseDelivery.UNDELIVERABLE
        )

    def test_scheduling_without_execution_is_not_delivery(self) -> None:
        """A callback that never runs must not report success."""
        server = _server()
        server._delivery_queues["c1"] = [object()]
        server.DELIVERY_ACK_SECONDS = 0.2

        class NeverRuns:
            def is_closed(self):
                return False

            def call_soon_threadsafe(self, callback):
                return None      # accepted, never executed

        server._delivery_loop = NeverRuns()
        self.assertIs(
            server.deliver("c1", "her words"), ResponseDelivery.UNDELIVERABLE
        )


class UnknownConversationRefusesTests(unittest.TestCase):
    """A thought with no thread must not be given someone else's."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.store = SQLiteContinuityStore(root / "c.sqlite3")
        self.addCleanup(self.store.close)
        self.ledger = SQLiteOpportunityLedger(root / "o.sqlite3")
        self.addCleanup(self.ledger.close)

    def _legacy_request(self) -> None:
        """A row written before conversation_id existed, as migration leaves it."""
        self.store.create(
            FutureCognitionRequest(
                "legacy", DUE, "an older thought", NOW - timedelta(days=1)
            )
        )

    def _run(self, gateway) -> tuple:
        source = FutureCognitionSource(
            self.store, self.ledger, enabled=True, clock=lambda: NOW
        )
        runner = AutonomousCognitionRunner(source, self.ledger, gateway, 4, 3650)
        return tuple(
            opportunity.opportunity_id
            for opportunity in source.due_opportunities()
            if runner.run_one(opportunity)
        )

    def test_a_migrated_request_makes_no_core_call(self) -> None:
        class NeverCalled:
            def receive_cognition_opportunity(self, *args, **kwargs):
                raise AssertionError("unknown provenance must not reach the Core")

        self._legacy_request()
        self.assertEqual(self._run(NeverCalled()), ())

    def test_it_never_reaches_any_other_conversation(self) -> None:
        seen: list[str] = []

        class Recording:
            def receive_cognition_opportunity(self, conversation_id, *args, **kw):
                seen.append(conversation_id)
                raise AssertionError("should not be reached")

        self._legacy_request()
        self._run(Recording())
        self.assertEqual(seen, [])

    def test_the_refusal_is_durable_and_visible(self) -> None:
        self._legacy_request()
        self._run(object())
        row = self.ledger.rows()[0]
        self.assertEqual(row["outcome"], "refused_unknown_conversation")
        self.assertIsNone(row["settled_usd"])

    def test_it_spends_nothing(self) -> None:
        self._legacy_request()
        self._run(object())
        row = self.ledger.rows()[0]
        self.assertIsNone(row["reserved_usd"])

    def test_recovery_does_not_re_offer_it_later(self) -> None:
        """Restart must not let it attach to a thread on a second attempt."""
        self._legacy_request()
        self._run(object())
        source = FutureCognitionSource(
            self.store, self.ledger, enabled=True, clock=lambda: NOW
        )
        self.assertEqual(source.due_opportunities(), ())
        self.assertEqual(source.recover(), ())

    def test_a_valid_conversation_still_resumes_itself(self) -> None:
        seen: list[str] = []

        class Recording:
            def receive_cognition_opportunity(self, conversation_id, *args, **kw):
                seen.append(conversation_id)

                class Outcome:
                    class state:
                        value = "finished_silently"
                    response = None

                return Outcome()

        self.store.create(
            FutureCognitionRequest(
                "r1", DUE, "a thought", NOW - timedelta(hours=1),
                conversation_id="conversation-A",
            )
        )
        self._run(Recording())
        self.assertEqual(seen, ["conversation-A"])


if __name__ == "__main__":
    unittest.main()
