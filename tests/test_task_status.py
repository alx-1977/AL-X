"""Watching external work: honest states, and a watcher that cannot act.

The point of this is visibility. Friedl asks for a review, and until the result
appears he should be able to see that something is outstanding rather than
wonder whether anything happened.

Two properties matter more than the display. A state is claimed only where
something observable supports it - there is no "running", because nothing
distinguishes a service that is thinking from one that has not started. And the
watcher observes: it cannot request a review, retry, spend, fix or merge, and
that is asserted structurally rather than trusted.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.tasks import build_task_runtime  # noqa: E402
from alx.continuity.tasks import SQLiteTaskStore, TaskStoreCorrupt  # noqa: E402
from alx.contracts.cognition import CognitionOrigin  # noqa: E402
from alx.contracts.task import ExternalTask, TaskObservation, TaskState  # noqa: E402
from alx.interfaces.task_poller import TaskPoller  # noqa: E402
from alx.providers.qodo_status import (  # noqa: E402
    QodoStatusObserver,
    subject_reference,
)
from tests.qodo_transcript import (  # noqa: E402
    GitHubTranscript,
    PUBLISHED_AT,
    realistic_issue_comments,
)


HEAD = "a" * 40
OTHER = "b" * 40
QODO = 151058649


def _task(state: TaskState = TaskState.REQUESTED, **overrides) -> ExternalTask:
    values = dict(
        task_id=f"review:21:{HEAD}",
        kind="external_review",
        service="qodo",
        subject_reference=subject_reference(21, HEAD),
        state=state,
        requested_at=datetime.now(UTC) - timedelta(seconds=90),
        conversation_id="conversation-1",
    )
    values.update(overrides)
    return ExternalTask(**values)


class RecordingObserver:
    def __init__(self, *states: TaskState) -> None:
        self._states = list(states)
        self.looks = 0

    def observe(self, subject: str, since=None) -> TaskObservation:
        self.looks += 1
        state = self._states.pop(0) if self._states else TaskState.WAITING_FOR_RESULT
        return TaskObservation(state, datetime.now(UTC))


class PollerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteTaskStore(Path(self.directory.name) / "tasks.sqlite3")
        self.lines: list[tuple[str, dict]] = []
        self.woken: list[ExternalTask] = []

    def _poller(self, observer, service: str = "qodo") -> TaskPoller:
        return TaskPoller(
            self.store,
            {service: observer},
            interval_seconds=1.0,
            announce=lambda conversation, values: self.lines.append(
                (conversation, values)
            ),
            completed=self.woken.append,
        )


class FatalWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_corruption_stops_the_watcher(self) -> None:
        class CorruptStore:
            @staticmethod
            def outstanding():
                raise TaskStoreCorrupt("unreadable")

        poller = TaskPoller(
            CorruptStore(),
            {},
            interval_seconds=1.0,
            announce=lambda conversation, values: None,
            completed=lambda task: None,
            fatal_exceptions=(TaskStoreCorrupt,),
        )
        with self.assertRaises(TaskStoreCorrupt):
            await poller.run()


class TaskStateTests(unittest.TestCase):
    """The states say only what can be observed."""

    def test_there_is_no_running_state(self) -> None:
        """A service that is thinking looks exactly like one that has not begun."""
        self.assertEqual(
            {state.value for state in TaskState},
            {
                "requested",
                "waiting_for_result",
                "completed",
                "failed",
                "status_unknown",
                "observer_unavailable",
            },
        )

    def test_terminal_states_stop_watching_a_task(self) -> None:
        self.assertTrue(TaskState.COMPLETED.is_settled)
        self.assertTrue(TaskState.FAILED.is_settled)
        self.assertTrue(TaskState.OBSERVER_UNAVAILABLE.is_settled)
        for state in (
            TaskState.REQUESTED,
            TaskState.WAITING_FOR_RESULT,
            TaskState.STATUS_UNKNOWN,
        ):
            self.assertFalse(state.is_settled)

    def test_a_completed_task_records_when_it_completed(self) -> None:
        with self.assertRaises(ValueError):
            _task(state=TaskState.COMPLETED)

    def test_a_failed_task_records_when_it_finished(self) -> None:
        with self.assertRaises(ValueError):
            _task(state=TaskState.FAILED)

    def test_elapsed_stops_at_completion(self) -> None:
        finished = datetime.now(UTC)
        task = _task(
            state=TaskState.COMPLETED,
            requested_at=finished - timedelta(seconds=30),
            completed_at=finished,
        )
        self.assertAlmostEqual(
            task.elapsed_seconds(finished + timedelta(seconds=600)), 30.0, places=0
        )


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tasks.sqlite3"

    def test_an_outstanding_task_survives_a_restart(self) -> None:
        """The service keeps working when the process stops."""
        SQLiteTaskStore(self.path).record(_task())
        self.assertEqual(len(SQLiteTaskStore(self.path).outstanding()), 1)

    def test_a_settled_task_stops_being_watched(self) -> None:
        store = SQLiteTaskStore(self.path)
        store.record(_task())
        store.record(
            _task(state=TaskState.COMPLETED, completed_at=datetime.now(UTC))
        )
        self.assertEqual(store.outstanding(), ())

    def test_a_corrupt_outstanding_row_fails_closed(self) -> None:
        import sqlite3

        store = SQLiteTaskStore(self.path)
        store.record(_task(task_id="valid-task"))
        database = sqlite3.connect(self.path)
        database.execute(
            "INSERT INTO external_tasks (task_id, kind, service, "
            "subject_reference, state, requested_at, conversation_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "corrupt-task",
                "external_review",
                "qodo",
                subject_reference(21),
                "invented_state",
                datetime.now(UTC).isoformat(),
                "conversation-1",
            ),
        )
        database.commit()
        database.close()

        with self.assertRaisesRegex(TaskStoreCorrupt, "corrupt-task"):
            store.outstanding()

    def test_a_corrupt_completed_row_cannot_vanish_from_recovery(self) -> None:
        import sqlite3

        store = SQLiteTaskStore(self.path)
        database = sqlite3.connect(self.path)
        database.execute(
            "INSERT INTO external_tasks (task_id, kind, service, "
            "subject_reference, state, requested_at, completed_at, "
            "conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "corrupt-completed-task",
                "external_review",
                "qodo",
                subject_reference(21, HEAD),
                TaskState.COMPLETED.value,
                "not-a-timestamp",
                datetime.now(UTC).isoformat(),
                "conversation-1",
            ),
        )
        database.commit()
        database.close()

        with self.assertRaisesRegex(TaskStoreCorrupt, "corrupt-completed-task"):
            store.completed_unhandled()

    def test_a_database_that_becomes_unreadable_uses_the_store_contract(self) -> None:
        import sqlite3

        store = SQLiteTaskStore(self.path)

        def unavailable():
            raise sqlite3.DatabaseError("unreadable")

        store._db = unavailable
        with self.assertRaises(TaskStoreCorrupt):
            store.outstanding()

    def test_reusing_an_identifier_reopens_the_handoff_honestly(self) -> None:
        store = SQLiteTaskStore(self.path)
        finished = datetime.now(UTC)
        store.record(
            _task(
                task_id="same-id",
                state=TaskState.COMPLETED,
                completed_at=finished,
            )
        )
        store.mark_handed_over("same-id")
        store.record(
            _task(
                task_id="same-id",
                subject_reference=subject_reference(22),
                conversation_id="conversation-2",
            )
        )
        reopened = store.outstanding()[0]
        self.assertEqual(reopened.subject_reference, subject_reference(22))
        self.assertEqual(reopened.conversation_id, "conversation-2")


class PollerTests(PollerHarness):
    def test_record_announces_the_requested_state_immediately(self) -> None:
        task = _task()
        self._poller(RecordingObserver()).record(task)
        self.assertEqual(self.lines[0][1]["task_id"], task.task_id)
        self.assertEqual(self.lines[0][1]["state"], "requested")

    def test_waiting_reports_elapsed_time_and_keeps_watching(self) -> None:
        self.store.record(_task())
        observer = RecordingObserver(TaskState.WAITING_FOR_RESULT)
        self._poller(observer).tick()

        self.assertEqual(len(self.store.outstanding()), 1)
        self.assertEqual(self.woken, [])
        conversation, values = self.lines[0]
        self.assertEqual(conversation, "conversation-1")
        # A state and a duration, so the terminal can show a live row rather
        # than a line that scrolls away. Not a claim about progress.
        self.assertEqual(values["state"], "waiting_for_result")
        self.assertEqual(values["subject"], f"PR #21 @ {HEAD[:7]}")
        self.assertEqual(values["service"], "qodo")
        self.assertIsInstance(values["elapsed_seconds"], int)
        self.assertGreaterEqual(values["elapsed_seconds"], 90)

    def test_a_result_completes_the_task_and_wakes_the_core(self) -> None:
        self.store.record(_task())
        self._poller(RecordingObserver(TaskState.COMPLETED)).tick()

        self.assertEqual(self.store.outstanding(), ())
        self.assertEqual(len(self.woken), 1)
        self.assertIs(self.woken[0].state, TaskState.COMPLETED)
        self.assertIsNotNone(self.woken[0].completed_at)
        self.assertEqual(self.lines[0][1]["subject"], f"PR #21 @ {HEAD[:7]}")
        self.assertEqual(self.lines[0][1]["state"], "completed")

    def test_a_failure_finishes_the_task_and_wakes_the_core(self) -> None:
        self.store.record(_task())
        self._poller(RecordingObserver(TaskState.FAILED)).tick()

        self.assertEqual(self.store.outstanding(), ())
        self.assertEqual(len(self.woken), 1)
        self.assertIs(self.woken[0].state, TaskState.FAILED)
        self.assertIsNotNone(self.woken[0].completed_at)
        self.assertEqual(self.lines[0][1]["state"], "failed")
        self.assertEqual(len(self.store.completed_unhandled()), 1)

    def test_completion_replaces_a_pr_level_subject_with_the_reviewed_sha(self) -> None:
        task = _task(subject_reference=subject_reference(21))
        self.store.record(task)

        class ResolvingObserver:
            def observe(self, subject, since=None):
                return TaskObservation(
                    TaskState.COMPLETED,
                    datetime.now(UTC),
                    subject_reference(21, HEAD),
                )

        self._poller(ResolvingObserver()).tick()
        self.assertEqual(self.woken[0].subject_reference, subject_reference(21, HEAD))
        self.assertEqual(self.lines[0][1]["subject"], f"PR #21 @ {HEAD[:7]}")

    def test_the_core_is_woken_with_the_task_and_never_the_result(self) -> None:
        """What the review says is hers to read from the source."""
        self.store.record(_task())
        self._poller(RecordingObserver(TaskState.COMPLETED)).tick()
        task = self.woken[0]
        self.assertEqual(task.subject_reference, subject_reference(21, HEAD))
        self.assertFalse(hasattr(task, "findings"))
        self.assertFalse(hasattr(task, "body"))

    def test_a_completed_task_is_not_reported_twice(self) -> None:
        """A second tick must not wake the Core again for the same result."""
        self.store.record(_task())
        poller = self._poller(RecordingObserver(TaskState.COMPLETED))
        poller.tick()
        poller.tick()
        self.assertEqual(len(self.woken), 1)

    def test_an_unreadable_service_is_reported_as_unknown(self) -> None:
        self.store.record(_task())
        self._poller(RecordingObserver(TaskState.STATUS_UNKNOWN)).tick()
        outstanding = self.store.outstanding()
        self.assertIs(outstanding[0].state, TaskState.STATUS_UNKNOWN)
        self.assertEqual(self.lines[0][1]["state"], "status_unknown")
        self.assertEqual(self.woken, [])

    def test_a_removed_observer_retires_the_task_without_repeated_writes(self) -> None:
        self.store.record(_task(service="unwatched"))
        observer = RecordingObserver(TaskState.COMPLETED)
        poller = self._poller(observer)
        poller.tick()
        first_lines = tuple(self.lines)
        poller.tick()
        self.assertEqual(observer.looks, 0)
        self.assertEqual(self.store.outstanding(), ())
        self.assertEqual(first_lines, tuple(self.lines))
        self.assertEqual(first_lines[0][1]["state"], "observer_unavailable")

    def test_a_returning_observer_restores_its_retained_tasks(self) -> None:
        self.store.record(_task(service="qodo"))
        TaskPoller(
            self.store,
            {},
            interval_seconds=1.0,
            announce=lambda conversation, values: None,
            completed=lambda task: None,
        ).tick()
        self.assertEqual(self.store.outstanding(), ())

        self.store.restore_observers(frozenset({"qodo"}))
        restored = self.store.outstanding()
        self.assertEqual(len(restored), 1)
        self.assertIs(restored[0].state, TaskState.REQUESTED)
        self.assertIsNone(restored[0].last_checked_at)

    def test_startup_restores_tasks_for_observers_that_returned(self) -> None:
        startup_store = SQLiteTaskStore(
            Path(self.directory.name) / "external-tasks.sqlite3"
        )
        startup_store.record(_task(service="qodo"))
        TaskPoller(
            startup_store,
            {},
            interval_seconds=1.0,
            announce=lambda conversation, values: None,
            completed=lambda task: None,
        ).tick()

        runtime = build_task_runtime(
            Path(self.directory.name),
            "",
            "",
            announce=lambda conversation, values: None,
            completed=lambda task: None,
            observers={"qodo": RecordingObserver(TaskState.COMPLETED)},
        )

        self.assertIsNotNone(runtime)
        restored = runtime.store.outstanding()
        self.assertEqual(len(restored), 1)
        self.assertIs(restored[0].state, TaskState.REQUESTED)

    def test_the_terminal_line_carries_no_finding_text(self) -> None:
        """D-012: identifiers, states and durations only."""
        self.store.record(_task())
        self._poller(RecordingObserver(TaskState.COMPLETED)).tick()
        for _, values in self.lines:
            # Exactly the permitted fields, so nothing a reviewer wrote can
            # ride along in a payload that quietly grew a field.
            self.assertEqual(
                set(values),
                {"task_id", "state", "subject", "service", "elapsed_seconds"},
            )
            rendered = " ".join(str(value) for value in values.values()).lower()
            self.assertNotIn("bug", rendered)
            self.assertNotIn("severity", rendered)
            self.assertLess(len(rendered), 100)


class WatcherCannotActTests(unittest.TestCase):
    """Polling is for visibility. It must be incapable of anything else."""

    MODULES = (
        "src/alx/interfaces/task_poller.py",
        "src/alx/providers/qodo_status.py",
        "src/alx/providers/qodo_artifact.py",
        "src/alx/continuity/tasks.py",
    )

    def test_the_watcher_cannot_request_a_review_or_merge(self) -> None:
        for relative in self.MODULES:
            with self.subTest(module=relative):
                tree = ast.parse((REPOSITORY_ROOT / relative).read_text())
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        names = [alias.name for alias in node.names]
                        names.append(getattr(node, "module", "") or "")
                        joined = " ".join(names)
                        self.assertNotIn("qodo_review", joined)
                        self.assertNotIn("github_merge", joined)
                        self.assertNotIn("tools.review", joined)
                        self.assertNotIn("tools.repository", joined)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertNotEqual(node.value, "/review")
                        self.assertNotEqual(node.value, "merge_pull_request")

    def test_the_observer_only_reads(self) -> None:
        """No write verb reaches GitHub from the status path."""
        calls = []
        for relative in (
            "src/alx/providers/qodo_status.py",
            "src/alx/providers/qodo_artifact.py",
        ):
            tree = ast.parse((REPOSITORY_ROOT / relative).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id == "httpx":
                        calls.append(target.attr)
                        self.assertEqual(target.attr, "get")
        self.assertEqual(calls, ["get"])


class QodoObserverTests(unittest.TestCase):
    """The completion rule Friedl approved, and nothing beyond it."""

    def _observer(self, reviews, comments):
        from alx.providers import qodo_artifact

        normalised = []
        for index, review in enumerate(reviews, 1):
            item = dict(review)
            item.setdefault("id", index)
            item.setdefault("body", "Review complete.")
            item.setdefault("submitted_at", PUBLISHED_AT)
            normalised.append(item)
        transcript = GitHubTranscript(issue_comments=comments, reviews=normalised)
        original = qodo_artifact.httpx.get
        qodo_artifact.httpx.get = transcript.get
        self.addCleanup(setattr, qodo_artifact.httpx, "get", original)
        return QodoStatusObserver("owner/repo", "token")

    def test_a_repository_that_cannot_build_a_url_is_refused(self) -> None:
        """The same rule as the review and merge providers, for one reason.

        A value that passes a non-blank check registers the observer and then
        makes every read malformed, which surfaces only as a task that never
        resolves. Refusing at construction makes the misconfiguration visible
        where it can be fixed.
        """
        for repository in (
            "own er/repo",
            "owner/re?po",
            "owner/repo#x",
            "owner/",
            "owner/repo/extra",
            "../../etc",
        ):
            with self.subTest(repository=repository):
                with self.assertRaises(ValueError):
                    QodoStatusObserver(repository, "token")

        self.assertIsNotNone(QodoStatusObserver("owner/repo", "token"))

    def test_a_review_object_at_the_exact_revision_completes(self) -> None:
        observer = self._observer(
            [{"user": {"id": QODO}, "commit_id": HEAD}], []
        )
        self.assertIs(
            observer.observe(subject_reference(21, HEAD)).state, TaskState.COMPLETED
        )

    def test_a_review_object_at_another_revision_does_not_complete(self) -> None:
        observer = self._observer(
            [{"user": {"id": QODO}, "commit_id": OTHER}], []
        )
        self.assertIs(
            observer.observe(subject_reference(21, HEAD)).state,
            TaskState.WAITING_FOR_RESULT,
        )

    def test_an_updated_marker_naming_the_revision_completes(self) -> None:
        """A clean review publishes no review object, so this is the only signal."""
        observer = self._observer(
            [],
            realistic_issue_comments(HEAD),
        )
        self.assertIs(
            observer.observe(subject_reference(21, HEAD)).state, TaskState.COMPLETED
        )

    def test_a_qodo_comment_about_another_revision_does_not_complete(self) -> None:
        """The marker must name this revision, not merely be Qodo's.

        Qodo comments on a pull request many times. Treating any of its
        comments as a result would report a review of a revision that was
        never examined.
        """
        observer = self._observer(
            [],
            [
                {"user": {"id": QODO}, "body": "Qodo is busy working"},
                {
                    "user": {"id": QODO},
                    "body": f"updated up to https://github.com/o/r/commit/{OTHER}",
                },
            ],
        )
        self.assertIs(
            observer.observe(subject_reference(21, HEAD)).state,
            TaskState.WAITING_FOR_RESULT,
        )

    def test_another_accounts_comment_naming_the_revision_does_not_complete(
        self,
    ) -> None:
        observer = self._observer(
            [], [{"user": {"id": 1}, "body": f"see /commit/{HEAD}"}]
        )
        self.assertIs(
            observer.observe(subject_reference(21, HEAD)).state,
            TaskState.WAITING_FOR_RESULT,
        )

    def test_unreadable_state_is_unknown_rather_than_waiting(self) -> None:
        from alx.providers import qodo_status

        def refuse(url, headers, timeout):
            raise qodo_status.httpx.HTTPError("no")

        original = qodo_status.httpx.get
        qodo_status.httpx.get = refuse
        self.addCleanup(setattr, qodo_status.httpx, "get", original)
        observer = QodoStatusObserver("owner/repo", "token")
        self.assertIs(
            observer.observe(subject_reference(21, HEAD)).state,
            TaskState.STATUS_UNKNOWN,
        )

    def test_a_subject_that_cannot_be_read_is_unknown(self) -> None:
        observer = self._observer([], [])
        for subject in ("", f"pull/21@{HEAD[:12]}", "nonsense"):
            with self.subTest(subject=subject):
                self.assertIs(
                    observer.observe(subject).state, TaskState.STATUS_UNKNOWN
                )


class ReviewFindingRegressions(unittest.TestCase):
    """The defects an external review found in the watcher, kept closed."""

    def _observer(self, reviews, comments):
        return QodoObserverTests._observer(self, reviews, comments)

    def test_a_result_older_than_the_request_does_not_complete_it(self) -> None:
        """Asking again for an unchanged revision is a new occasion.

        Without this the previous answer completes the new request the moment
        it is made, and the terminal reports a review that never ran.
        """
        asked = datetime.now(UTC)
        observer = self._observer(
            [
                {
                    "user": {"id": QODO},
                    "commit_id": HEAD,
                    "submitted_at": (asked - timedelta(hours=1)).isoformat(),
                }
            ],
            [],
        )
        self.assertIs(
            observer.observe(subject_reference(21, HEAD), asked).state,
            TaskState.WAITING_FOR_RESULT,
        )

    def test_a_result_after_the_request_completes_it(self) -> None:
        asked = datetime.now(UTC)
        observer = self._observer(
            [
                {
                    "user": {"id": QODO},
                    "commit_id": HEAD,
                    "submitted_at": (asked + timedelta(minutes=1)).isoformat(),
                }
            ],
            [],
        )
        self.assertIs(
            observer.observe(subject_reference(21, HEAD), asked).state,
            TaskState.COMPLETED,
        )

    def test_a_result_on_a_later_page_is_still_found(self) -> None:
        """A busy pull request outgrows one page of history."""
        from alx.providers import qodo_artifact

        class Response:
            def __init__(self, body):
                self.status_code = 200
                self._body = body

            def json(self):
                return self._body

        def get(url, headers, timeout):
            if "/pulls/21/reviews" not in url or "/comments" in url:
                return Response([])
            page = url.rsplit("page=", 1)[-1]
            if page == "1":
                # A full page, so the reader continues to the next.
                return Response([{"user": {"id": 1}, "commit_id": OTHER}] * 100)
            if page == "2":
                return Response([{
                    "id": 101,
                    "user": {"id": QODO},
                    "commit_id": HEAD,
                    "body": "Review complete.",
                    "submitted_at": PUBLISHED_AT,
                }])
            return Response([])

        original = qodo_artifact.httpx.get
        qodo_artifact.httpx.get = get
        self.addCleanup(setattr, qodo_artifact.httpx, "get", original)
        observer = QodoStatusObserver("owner/repo", "token")
        self.assertIs(
            observer.observe(subject_reference(21, HEAD)).state, TaskState.COMPLETED
        )


class PollerFailureRegressions(PollerHarness):
    def test_a_failed_wake_leaves_the_task_outstanding(self) -> None:
        """A result must not be lost because reporting it failed.

        Recording completion before announcing meant a callback failure
        settled the task in the store and never reported it, so no later tick
        and no restart would try again.
        """
        self.store.record(_task())

        def explode(task):
            raise RuntimeError("wake failed")

        poller = TaskPoller(
            self.store,
            {"qodo": RecordingObserver(TaskState.COMPLETED)},
            interval_seconds=1.0,
            announce=lambda conversation, values: self.lines.append(
                (conversation, values)
            ),
            completed=explode,
        )
        with self.assertRaises(RuntimeError):
            poller.tick()
        # Still outstanding, so the next tick tries again.
        self.assertEqual(len(self.store.outstanding()), 1)

    def test_one_failed_task_does_not_prevent_another_completing(self) -> None:
        first = _task(task_id="first")
        second = _task(task_id="second")
        self.store.record(first)
        self.store.record(second)

        class MixedObserver:
            def observe(self, subject, since=None):
                if subject == first.subject_reference:
                    raise RuntimeError("unreadable")
                return TaskObservation(TaskState.COMPLETED, datetime.now(UTC))

        # Give the tasks distinct subjects so the observer can distinguish them.
        self.store.record(
            _task(task_id="second", subject_reference=subject_reference(22, HEAD))
        )
        with self.assertRaises(RuntimeError):
            self._poller(MixedObserver()).tick()
        self.assertEqual([task.task_id for task in self.woken], ["second"])


class WatchIdentityTests(unittest.TestCase):
    def test_same_second_requests_receive_distinct_task_identifiers(self) -> None:
        from alx.bootstrap.live_voice import _watch_review

        captured: list[ExternalTask] = []

        class Poller:
            def record(self, task):
                captured.append(task)

        class Runtime:
            poller = Poller()

        requested_at = datetime.now(UTC)
        for _ in range(2):
            _watch_review(Runtime(), "conversation-1", 21, HEAD, requested_at)
        self.assertEqual(len(captured), 2)
        self.assertNotEqual(captured[0].task_id, captured[1].task_id)
        self.assertEqual(captured[0].subject_reference, subject_reference(21, HEAD))

    def test_unknown_head_keeps_the_review_reference_unpinned(self) -> None:
        from alx.bootstrap.live_voice import _watch_review

        captured: list[ExternalTask] = []

        class Poller:
            def record(self, task):
                captured.append(task)

        class Runtime:
            poller = Poller()

        _watch_review(Runtime(), "conversation-1", 21, "", datetime.now(UTC))
        self.assertEqual(captured[0].subject_reference, subject_reference(21))


class OneProducerForEveryOccasionTest(unittest.TestCase):
    """Both kinds of occasion reach the Core through one tick and one runner."""

    class Ledger:
        def __init__(self) -> None:
            self.created: list[str] = []

        def exists(self, opportunity_id: str) -> bool:
            return opportunity_id in self.created

        def record_created(self, opportunity) -> bool:
            if opportunity.opportunity_id in self.created:
                return False
            self.created.append(opportunity.opportunity_id)
            return True

        def release(self, opportunity_id: str) -> None:
            self.created.remove(opportunity_id)

    def test_each_occasion_is_claimed_by_the_producer_that_made_it(self) -> None:
        """A combined producer must not blur which source owns what.

        Claiming through the wrong producer would mark the wrong thing
        honoured, and the real one would be offered again as a second paid
        turn.
        """
        from alx.continuity import CompletedWorkSource
        from alx.continuity.occasions import CombinedOccasionSource

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SQLiteTaskStore(Path(directory.name) / "tasks.sqlite3")
        store.record(_task())
        store.record(
            _task(state=TaskState.COMPLETED, completed_at=datetime.now(UTC))
        )

        class Matured:
            enabled = True
            honoured: list = []

            def due_opportunities(self):
                return ()

            def owns(self, opportunity) -> bool:
                return any(
                    r.startswith("future_cognition:") for r in opportunity.references
                )

            def claim(self, opportunity) -> bool:
                return True

            def release(self, opportunity) -> None:
                pass

            def mark_honoured(self, opportunity) -> None:
                Matured.honoured.append(opportunity)

        work = CompletedWorkSource(store, self.Ledger(), enabled=True)
        combined = CombinedOccasionSource(Matured(), work)

        occasions = combined.due_opportunities()
        self.assertEqual(len(occasions), 1)
        self.assertTrue(combined.claim(occasions[0]))
        combined.mark_honoured(occasions[0])

        # The task producer closed its own work; the other was never touched.
        self.assertEqual(store.completed_unhandled(), ())
        self.assertEqual(Matured.honoured, [])

    def test_an_unowned_occasion_is_never_claimed(self) -> None:
        """Nothing is spent on an occasion no producer recognises."""
        from alx.continuity.occasions import CombinedOccasionSource
        from alx.contracts.continuity import CognitionOpportunity

        combined = CombinedOccasionSource()
        stray = CognitionOpportunity(
            opportunity_id="stray",
            origin=CognitionOrigin.WORK_COMPLETED,
            arose_at=datetime.now(UTC),
            conversation_id="c",
            references=("something_else:1",),
        )
        self.assertFalse(combined.claim(stray))


if __name__ == "__main__":
    unittest.main()


class StoreUpgradeTest(unittest.TestCase):
    """A store written before the handover column keeps working.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so the
    column was missing on every database created before it was added. The
    handover query then raised on each tick, and a completed task was never
    given to the Core - the same silence the handover exists to end, arriving
    by a different route. Found on the real runtime store rather than here,
    because every test until now built a fresh one.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tasks.sqlite3"

    def _write_old_schema(self) -> None:
        """The table exactly as it was before the column existed."""
        import sqlite3

        database = sqlite3.connect(self.path)
        database.executescript(
            """
            CREATE TABLE external_tasks (
                task_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                service TEXT NOT NULL,
                subject_reference TEXT NOT NULL,
                state TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                last_checked_at TEXT,
                completed_at TEXT,
                conversation_id TEXT NOT NULL DEFAULT ''
            );
            """
        )
        finished = datetime.now(UTC)
        database.execute(
            "INSERT INTO external_tasks (task_id, kind, service, "
            "subject_reference, state, requested_at, completed_at, "
            "conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"review:21:{HEAD}",
                "external_review",
                "qodo",
                subject_reference(21, HEAD),
                TaskState.COMPLETED.value,
                (finished - timedelta(seconds=60)).isoformat(),
                finished.isoformat(),
                "conversation-1",
            ),
        )
        database.commit()
        database.close()

    def _columns(self) -> set[str]:
        import sqlite3

        database = sqlite3.connect(self.path)
        try:
            return {
                item[1]
                for item in database.execute("PRAGMA table_info(external_tasks)")
            }
        finally:
            database.close()

    def test_an_older_store_gains_the_column_and_keeps_its_rows(self) -> None:
        self._write_old_schema()
        self.assertNotIn("handed_over", self._columns())

        store = SQLiteTaskStore(self.path)

        # The column is there now.
        self.assertIn("handed_over", self._columns())

        # And the row that was already there survived the upgrade intact.
        outstanding_and_done = store.completed_unhandled()
        self.assertEqual(len(outstanding_and_done), 1)
        task = outstanding_and_done[0]
        self.assertEqual(task.task_id, f"review:21:{HEAD}")
        self.assertEqual(task.conversation_id, "conversation-1")
        self.assertIs(task.state, TaskState.COMPLETED)
        self.assertIsNotNone(task.completed_at)

    def test_the_handover_works_after_the_upgrade(self) -> None:
        """The query that used to raise now answers, and can be closed."""
        self._write_old_schema()
        store = SQLiteTaskStore(self.path)

        self.assertEqual(len(store.completed_unhandled()), 1)
        store.mark_handed_over(f"review:21:{HEAD}")
        self.assertEqual(store.completed_unhandled(), ())

    def test_opening_an_upgraded_store_again_changes_nothing(self) -> None:
        """The migration runs once and is harmless afterwards."""
        self._write_old_schema()
        SQLiteTaskStore(self.path)
        store = SQLiteTaskStore(self.path)
        self.assertIn("handed_over", self._columns())
        self.assertEqual(len(store.completed_unhandled()), 1)


class CompletionReachesCoreTest(unittest.TestCase):
    """The handoff the watcher exists for, end to end.

    The watcher noticed completion and wrote an opportunity straight to the
    ledger, where nothing consumed it: the row said `created` forever and the
    Core was never invoked. The completion now goes through the same producer
    protocol a matured request uses, so the existing tick picks it up.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteTaskStore(Path(self.directory.name) / "tasks.sqlite3")

    class Ledger:
        """The occasion ledger, reduced to what the protocol touches."""

        def __init__(self) -> None:
            self.created: list[str] = []
            self.retained: list[str] = []

        def exists(self, opportunity_id: str) -> bool:
            return opportunity_id in self.created

        def record_created(self, opportunity) -> bool:
            if opportunity.opportunity_id in self.created:
                return False
            self.created.append(opportunity.opportunity_id)
            return True

        def release(self, opportunity_id: str) -> None:
            self.created.remove(opportunity_id)

        def unfinished(self):
            # Only rows that never reached a terminal outcome, as the real
            # ledger does. A retained row is terminal and never offered again.
            return tuple(
                {"opportunity_id": identifier}
                for identifier in self.created
                if identifier not in self.retained
            )

        def mark_unreconciled(self, opportunity_id: str) -> None:
            # Terminal, and the row stays: the real ledger only changes the
            # outcome, so the occasion still exists and is never re-offered.
            self.retained.append(opportunity_id)

    def _complete_one(self, state: TaskState = TaskState.COMPLETED) -> None:
        """Drive the watcher until it observes a terminal outcome."""
        self.store.record(_task())
        poller = TaskPoller(
            self.store,
            {"qodo": RecordingObserver(state)},
            interval_seconds=1.0,
            announce=lambda conversation, values: None,
            # Completion is durable in the store; nothing is written to the
            # ledger here, which is the defect this test exists to prove fixed.
            completed=lambda task: None,
        )
        poller.tick()

    def test_an_observed_completion_becomes_a_consumable_occasion(self) -> None:
        from alx.continuity import CompletedWorkSource

        self._complete_one()
        source = CompletedWorkSource(self.store, self.Ledger(), enabled=True)
        opportunities = source.due_opportunities()

        self.assertEqual(len(opportunities), 1)
        occasion = opportunities[0]
        self.assertIs(occasion.origin, CognitionOrigin.WORK_COMPLETED)
        self.assertEqual(occasion.conversation_id, "conversation-1")
        self.assertIsNotNone(occasion.arose_at)
        # The task, never the result.
        self.assertEqual(
            occasion.references, (f"external_task:review:21:{HEAD}",)
        )
        self.assertIsNone(occasion.note)

    def test_an_observed_failure_becomes_a_consumable_occasion(self) -> None:
        from alx.continuity import CompletedWorkSource

        self._complete_one(TaskState.FAILED)
        source = CompletedWorkSource(self.store, self.Ledger(), enabled=True)
        opportunities = source.due_opportunities()

        self.assertEqual(len(opportunities), 1)
        self.assertIs(opportunities[0].origin, CognitionOrigin.WORK_COMPLETED)
        self.assertEqual(opportunities[0].conversation_id, "conversation-1")

    def test_the_existing_tick_schedules_a_core_turn_for_it(self) -> None:
        """The whole point: the runtime that already exists runs the turn."""
        import asyncio

        from alx.continuity import CompletedWorkSource
        from alx.continuity.due_source import DueCognitionSource

        self._complete_one()
        source = CompletedWorkSource(self.store, self.Ledger(), enabled=True)
        ran: list = []

        class Runner:
            def run_one(self, opportunity) -> bool:
                # What the real runner does first, so the claim is exercised.
                if not source.claim(opportunity):
                    return False
                ran.append(opportunity)
                source.mark_honoured(opportunity)
                return True

        tick = DueCognitionSource(source, Runner(), asyncio.Lock(), 30.0)
        self.assertEqual(asyncio.run(tick.tick()), 1)
        self.assertEqual(len(ran), 1)
        self.assertIs(ran[0].origin, CognitionOrigin.WORK_COMPLETED)

    def test_a_handled_completion_is_not_run_twice(self) -> None:
        """A replayed occasion is a second paid turn for one result."""
        import asyncio

        from alx.continuity import CompletedWorkSource
        from alx.continuity.due_source import DueCognitionSource

        self._complete_one()
        ledger = self.Ledger()
        source = CompletedWorkSource(self.store, ledger, enabled=True)

        class Runner:
            def run_one(self, opportunity) -> bool:
                if not source.claim(opportunity):
                    return False
                source.mark_honoured(opportunity)
                return True

        tick = DueCognitionSource(source, Runner(), asyncio.Lock(), 30.0)
        self.assertEqual(asyncio.run(tick.tick()), 1)
        # The store itself must stop offering it. Checking only the source
        # would pass on the ledger's refusal alone, leaving the completion
        # unhandled forever in a fresh process whose ledger is empty.
        self.assertEqual(self.store.completed_unhandled(), ())
        self.assertEqual(source.due_opportunities(), ())
        self.assertEqual(asyncio.run(tick.tick()), 0)

        # And a restart, whose ledger has forgotten the claim, must not run it
        # again either.
        self.assertEqual(
            CompletedWorkSource(
                self.store, self.Ledger(), enabled=True
            ).due_opportunities(),
            (),
        )

    def test_a_claim_left_by_a_stopped_run_is_reclaimed(self) -> None:
        """Otherwise a result that arrived is hidden by its own claim.

        The claim is written before the turn runs. A process that stopped in
        between left the row behind, and every later scan skipped the task
        because the ledger said the occasion existed - so a completed review
        was watched, recorded, and then never looked at.
        """
        from alx.continuity import CompletedWorkSource

        self._complete_one()
        source = CompletedWorkSource(self.store, self.Ledger(), enabled=True)
        occasion = source.due_opportunities()[0]
        self.assertTrue(source.claim(occasion))
        # The stopped run: claimed, never handed over.
        self.assertEqual(source.due_opportunities(), ())

        self.assertEqual(source.recover(), (occasion.opportunity_id,))
        self.assertEqual(len(source.due_opportunities()), 1)

    def test_a_completion_that_reached_a_provider_is_never_replayed(self) -> None:
        """A duplicate paid turn is worse than one missed and visible."""
        from alx.continuity import CompletedWorkSource

        self._complete_one()
        ledger = self.Ledger()
        source = CompletedWorkSource(self.store, ledger, enabled=True)
        occasion = source.due_opportunities()[0]
        source.claim(occasion)

        class Spend:
            def dispatch_started(self, opportunity_id: str) -> bool:
                return True

        self.assertEqual(source.recover(Spend()), ())
        self.assertEqual(ledger.retained, [occasion.opportunity_id])
        self.assertEqual(source.due_opportunities(), ())

    def test_recovery_leaves_another_producers_occasions_alone(self) -> None:
        """The ledger is shared; the idempotence of each producer is not."""
        from alx.continuity import CompletedWorkSource

        ledger = self.Ledger()
        ledger.created.append("self:r1")
        source = CompletedWorkSource(self.store, ledger, enabled=True)

        self.assertEqual(source.recover(), ())
        self.assertEqual(ledger.created, ["self:r1"])

    def test_nothing_is_offered_while_the_switch_is_off(self) -> None:
        from alx.continuity import CompletedWorkSource

        self._complete_one()
        source = CompletedWorkSource(self.store, self.Ledger(), enabled=False)
        self.assertEqual(source.due_opportunities(), ())

    def test_an_incomplete_task_is_not_offered(self) -> None:
        from alx.continuity import CompletedWorkSource

        self.store.record(_task())
        source = CompletedWorkSource(self.store, self.Ledger(), enabled=True)
        self.assertEqual(source.due_opportunities(), ())
