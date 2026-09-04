"""Watching the mailbox is not a property of Friedl having a browser open.

Mail discovery and reconciliation used to run inside a voice exchange, so
closing the browser stopped them. Proven live on 2026-09-04: the runtime sat up
for forty seconds with no session and scanned nothing, and an observation
stranded an hour earlier stayed stranded because nothing was looking.

Ownership moved to the process, beside the transport rather than inside it,
exactly as the due-cognition tick already sat. The scan is mechanical: it
discovers, advances the cursor and reconciles, and it makes no Core call
however much mail it finds. Carrying a fact to AL/X remains the delivery path's
job, so nothing here decides whether mail matters.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alx.contracts import MailReference  # noqa: E402
from alx.providers.icloud_mail import (  # noqa: E402
    ICloudMailAdapter, SQLiteMailObservationState,
)
from alx.providers.mail_poller import MailPoller  # noqa: E402
from test_mail_vertical_slice import FakeImap, message  # noqa: E402


class CountingCore:
    """Stands in for everything a scan must never touch."""

    def __init__(self) -> None:
        self.calls = 0

    def process(self, *arguments, **keywords):
        self.calls += 1
        raise AssertionError("a scan must not invoke the Core")


class MailPollLifetimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "mail.sqlite3"
        self.state = SQLiteMailObservationState(self.path)
        self.imap = FakeImap()
        self.adapter = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            self.state, 1, connection_factory=lambda *a, **k: self.imap,
        )
        self.core = CountingCore()

    def poller(self, interval: float = 0.01) -> MailPoller:
        return MailPoller(self.adapter, interval, asyncio.Lock())

    def states(self) -> dict[int, str]:
        return {
            int(uid): value
            for uid, value in self.state._connection.execute(
                "SELECT uid, state FROM mail_observations"
            )
        }

    def cursor(self) -> int:
        return int(
            self.state._connection.execute(
                "SELECT last_uid FROM mail_cursor"
            ).fetchone()[0]
        )

    # -- A. no session, discovery continues ------------------------------

    async def test_discovery_continues_with_no_session(self) -> None:
        await self.poller().tick()
        self.imap.items[2] = message("Arrived alone", "body")
        await self.poller().tick()
        self.assertEqual(self.states()[2], "pending")
        self.assertEqual(self.cursor(), 2)
        self.assertEqual(self.core.calls, 0)

    async def test_the_tick_runs_repeatedly_without_a_session(self) -> None:
        poller = self.poller(0.01)
        # The first tick is the baseline that seeds the cursor, so mail is
        # added after it to prove later ticks keep finding things.
        await poller.tick()
        task = asyncio.create_task(poller.run())
        self.imap.items[2] = message("First", "body")
        await asyncio.sleep(0.08)
        self.imap.items[3] = message("Second", "body")
        await asyncio.sleep(0.08)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.states(), {2: "pending", 3: "pending"})
        self.assertEqual(self.core.calls, 0)

    # -- B. no session, reconciliation continues -------------------------

    async def test_reconciliation_continues_with_no_session(self) -> None:
        await self.poller().tick()
        self.imap.items[2] = message("Will vanish", "body")
        await self.poller().tick()
        event = self.state.current()
        self.state.record_delivery(event.event_id)

        del self.imap.items[2]
        await self.poller().tick()

        self.assertEqual(
            [e.data["uid"] for e in self.state.pending_vanished()], ["2"],
            "found gone while nobody was connected, and held for delivery",
        )
        self.assertEqual(self.states()[2], "presented")
        self.assertEqual(self.core.calls, 0)

    async def test_a_pending_ghost_settles_with_no_session(self) -> None:
        await self.poller().tick()
        self.imap.items[2] = message("Never announced", "body")
        await self.poller().tick()
        del self.imap.items[2]
        await self.poller().tick()
        self.assertEqual(self.states()[2], "done")
        self.assertEqual(self.state.pending_vanished(), ())
        self.assertEqual(self.core.calls, 0)

    # -- C. a session connects later -------------------------------------

    async def test_a_later_session_sees_what_was_found_without_it(self) -> None:
        await self.poller().tick()
        self.imap.items[2] = message("Found first", "body")
        await self.poller().tick()

        stream = self.adapter.events()
        try:
            event = await asyncio.wait_for(anext(stream), timeout=5)
        finally:
            await stream.aclose()
        self.assertEqual(event.data["uid"], "2")

    async def test_a_vanished_fact_found_alone_reaches_a_later_session(self) -> None:
        await self.poller().tick()
        self.imap.items[2] = message("Will vanish", "body")
        await self.poller().tick()
        delivered = self.state.current()
        self.state.record_delivery(delivered.event_id)
        del self.imap.items[2]
        await self.poller().tick()

        stream = self.adapter.events()
        try:
            event = await asyncio.wait_for(anext(stream), timeout=5)
        finally:
            await stream.aclose()
        self.assertEqual(event.kind, "mail.message_vanished")
        self.assertEqual(event.data["uid"], "2")

    async def test_a_carried_vanished_fact_is_not_carried_again(self) -> None:
        await self.poller().tick()
        self.imap.items[2] = message("Will vanish", "body")
        await self.poller().tick()
        delivered = self.state.current()
        self.state.record_delivery(delivered.event_id)
        del self.imap.items[2]
        await self.poller().tick()

        event = self.state.pending_vanished()[0]
        self.state.record_delivery(event.event_id)
        self.assertEqual(
            self.state.pending_vanished(), (),
            "a second session does not repeat what the first carried",
        )

    async def test_a_session_does_not_discover_anything_itself(self) -> None:
        """Law 0: delivery must not become a second scanner."""
        await self.poller().tick()
        self.imap.items[2] = message("Only the poller finds this", "body")
        before = len(self.imap.commands)

        stream = self.adapter.events()
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(anext(stream), timeout=0.2)
        finally:
            await stream.aclose()

        self.assertEqual(self.states(), {}, "the session discovered nothing")
        self.assertNotIn(
            ("UID", "search", None), self.imap.commands[before:],
            "and it never searched the mailbox",
        )

    # -- D. restart -------------------------------------------------------

    async def test_state_survives_restart_and_scanning_resumes_unattended(self) -> None:
        await self.poller().tick()
        self.imap.items[2] = message("Before restart", "body")
        await self.poller().tick()
        self.state.close()

        restarted = SQLiteMailObservationState(self.path)
        self.addCleanup(restarted.close)
        adapter = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            restarted, 1, connection_factory=lambda *a, **k: self.imap,
        )
        self.assertEqual(
            int(restarted._connection.execute(
                "SELECT last_uid FROM mail_cursor").fetchone()[0]), 2,
            "the cursor persists",
        )
        self.imap.items[3] = message("After restart", "body")
        await MailPoller(adapter, 0.01, asyncio.Lock()).tick()

        rows = dict(
            restarted._connection.execute("SELECT uid, state FROM mail_observations")
        )
        self.assertEqual(rows, {2: "pending", 3: "pending"})

    async def test_a_settled_observation_is_not_replayed_after_restart(self) -> None:
        await self.poller().tick()
        self.imap.items[2] = message("Handled", "body")
        await self.poller().tick()
        self.state.acknowledge(MailReference("INBOX", "777", "2"))
        self.state.close()

        restarted = SQLiteMailObservationState(self.path)
        self.addCleanup(restarted.close)
        adapter = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            restarted, 1, connection_factory=lambda *a, **k: self.imap,
        )
        await MailPoller(adapter, 0.01, asyncio.Lock()).tick()
        self.assertEqual(
            dict(restarted._connection.execute(
                "SELECT uid, state FROM mail_observations")),
            {2: "done"},
        )
        self.assertIsNone(restarted.current())

    # -- F. shutdown and cancellation -------------------------------------
    #
    # A CI job timed out here after 25,565 failed scans. `tick` waits for its
    # shielded worker so teardown cannot close the store under a running scan,
    # but `await worker` also *takes the worker's outcome*. When the scan
    # raised, its exception replaced the CancelledError, `run` handled it as an
    # ordinary scan failure, and the loop continued: the poller could not be
    # cancelled while scans were failing, so shutdown during a mailbox outage
    # would hang for ever.
    #
    # The property these four cases hold, together: once cancellation has been
    # requested the poller exits after the in-flight scan finishes, whether
    # that scan succeeded or raised, and the store never closes under it.

    async def blocked_scan(self, raises: bool):
        """A scan held on a worker thread until the test releases it."""
        entered = threading.Event()
        release = threading.Event()
        finished: list[str] = []

        class Blocking:
            def scan(inner) -> None:
                entered.set()
                release.wait(timeout=10)
                finished.append("scan")
                if raises:
                    raise RuntimeError("mailbox unavailable")

        return Blocking(), entered, release, finished

    async def until(self, flag: threading.Event) -> None:
        """Wait on the event loop, never on a pool worker the scan may need."""
        for _ in range(1000):
            if flag.is_set():
                return
            await asyncio.sleep(0.005)
        self.fail("the scan never reached its worker thread")

    # A. a normal successful scan leaves the poller running.

    async def test_a_successful_scan_leaves_the_poller_running(self) -> None:
        scans: list[int] = []

        class Adapter:
            def scan(inner) -> None:
                scans.append(1)

        poller = MailPoller(Adapter(), 0.001, asyncio.Lock())
        task = asyncio.create_task(poller.run())
        for _ in range(1000):
            if len(scans) >= 3:
                break
            await asyncio.sleep(0.005)
        self.assertFalse(task.done(), "it keeps polling")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreaterEqual(len(scans), 3)

    # B. a normal failed scan is reported and the poller keeps going.

    async def test_a_failed_scan_does_not_stop_the_tick(self) -> None:
        attempts: list[int] = []

        class FailingAdapter:
            def scan(inner) -> None:
                attempts.append(1)
                raise RuntimeError("mailbox unavailable")

        poller = MailPoller(FailingAdapter(), 0.001, asyncio.Lock())
        with self.assertLogs("alx.providers.mail_poller", "WARNING") as logs:
            task = asyncio.create_task(poller.run())
            for _ in range(1000):
                if len(attempts) >= 3:
                    break
                await asyncio.sleep(0.005)
            self.assertFalse(task.done(), "a failed scan is not a failed runtime")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(len(attempts), 3, "it keeps trying")
        self.assertTrue(any("Mail scan failed" in line for line in logs.output))

    # C. cancellation during a succeeding scan.

    async def test_cancellation_waits_for_a_succeeding_scan_then_exits(self) -> None:
        adapter, entered, release, finished = await self.blocked_scan(raises=False)
        lock = asyncio.Lock()
        poller = MailPoller(adapter, 0.001, lock)
        task = asyncio.create_task(poller.run())
        await self.until(entered)
        task.cancel()

        closed: list[str] = []

        async def teardown() -> None:
            async with lock:
                closed.append("closed")

        teardown_task = asyncio.create_task(teardown())
        await asyncio.sleep(0.05)
        self.assertFalse(closed, "the store must not close under a running scan")
        self.assertFalse(finished)

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        await teardown_task
        self.assertEqual(finished, ["scan"], "the scan was allowed to finish")
        self.assertEqual(closed, ["closed"], "and only then did the store close")

    # D. cancellation during a FAILING scan -- the defect CI found.

    async def test_cancellation_survives_a_failing_scan_and_stops_the_poller(self) -> None:
        adapter, entered, release, finished = await self.blocked_scan(raises=True)
        lock = asyncio.Lock()
        poller = MailPoller(adapter, 0.001, lock)
        task = asyncio.create_task(poller.run())
        await self.until(entered)
        task.cancel()

        closed: list[str] = []

        async def teardown() -> None:
            async with lock:
                closed.append("closed")

        teardown_task = asyncio.create_task(teardown())
        await asyncio.sleep(0.05)
        self.assertFalse(closed, "the store must not close under a running scan")

        release.set()
        # Promptly: the old behaviour looped here until the job timed out.
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        await teardown_task

        self.assertEqual(finished, ["scan"], "the failing scan still finished")
        self.assertEqual(closed, ["closed"], "and the store closed after it")
        self.assertTrue(task.done(), "no orphan poller task remains")
        self.assertEqual(
            len(finished), 1,
            "the worker's exception must not start another poll iteration",
        )

    async def test_a_cancelled_failing_scan_leaves_no_unretrieved_warning(self) -> None:
        """Its outcome is never retrieved, and that must stay silent.

        The scan's exception is deliberately not read: a provider exception can
        carry private runtime state and nothing may decide from it while the
        runtime is stopping (D-012). Discarding a future's exception can make
        asyncio complain at collection, so this proves it does not.
        """
        import gc

        adapter, entered, release, _finished = await self.blocked_scan(raises=True)
        poller = MailPoller(adapter, 0.001, asyncio.Lock())
        task = asyncio.create_task(poller.run())
        await self.until(entered)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        reported: list[dict] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: reported.append(context)
        )
        gc.collect()
        await asyncio.sleep(0.05)
        self.assertEqual(
            [item for item in reported if "never retrieved" in item.get("message", "")],
            [],
            "asyncio must not render the scan's exception and traceback",
        )


class MailPollerContractTest(unittest.TestCase):
    def test_the_interval_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            MailPoller(object(), 0, asyncio.Lock())


if __name__ == "__main__":
    unittest.main()
