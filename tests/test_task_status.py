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

from alx.continuity.tasks import SQLiteTaskStore  # noqa: E402
from alx.contracts.task import ExternalTask, TaskObservation, TaskState  # noqa: E402
from alx.interfaces.task_poller import TaskPoller  # noqa: E402
from alx.providers.qodo_status import (  # noqa: E402
    QodoStatusObserver,
    subject_reference,
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

    def observe(self, subject: str) -> TaskObservation:
        self.looks += 1
        state = self._states.pop(0) if self._states else TaskState.WAITING_FOR_RESULT
        return TaskObservation(state, datetime.now(UTC))


class PollerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteTaskStore(Path(self.directory.name) / "tasks.sqlite3")
        self.lines: list[tuple[str, str]] = []
        self.woken: list[ExternalTask] = []

    def _poller(self, observer, service: str = "qodo") -> TaskPoller:
        return TaskPoller(
            self.store,
            {service: observer},
            interval_seconds=1.0,
            announce=lambda conversation, line: self.lines.append(
                (conversation, line)
            ),
            completed=self.woken.append,
        )


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
            },
        )

    def test_only_completion_and_failure_settle_a_task(self) -> None:
        self.assertTrue(TaskState.COMPLETED.is_settled)
        self.assertTrue(TaskState.FAILED.is_settled)
        for state in (
            TaskState.REQUESTED,
            TaskState.WAITING_FOR_RESULT,
            TaskState.STATUS_UNKNOWN,
        ):
            self.assertFalse(state.is_settled)

    def test_a_completed_task_records_when_it_completed(self) -> None:
        with self.assertRaises(ValueError):
            _task(state=TaskState.COMPLETED)

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


class PollerTests(PollerHarness):
    def test_waiting_reports_elapsed_time_and_keeps_watching(self) -> None:
        self.store.record(_task())
        observer = RecordingObserver(TaskState.WAITING_FOR_RESULT)
        self._poller(observer).tick()

        self.assertEqual(len(self.store.outstanding()), 1)
        self.assertEqual(self.woken, [])
        conversation, line = self.lines[0]
        self.assertEqual(conversation, "conversation-1")
        self.assertTrue(line.startswith("Still waiting · "))
        # mm:ss, not a finding, not a claim about progress.
        self.assertRegex(line, r"Still waiting · \d\d:\d\d$")

    def test_a_result_completes_the_task_and_wakes_the_core(self) -> None:
        self.store.record(_task())
        self._poller(RecordingObserver(TaskState.COMPLETED)).tick()

        self.assertEqual(self.store.outstanding(), ())
        self.assertEqual(len(self.woken), 1)
        self.assertIs(self.woken[0].state, TaskState.COMPLETED)
        self.assertIsNotNone(self.woken[0].completed_at)
        self.assertEqual(self.lines[0][1], f"Review received · PR #21 @ {HEAD[:7]}")

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
        self.assertIn("Status unknown", self.lines[0][1])
        self.assertEqual(self.woken, [])

    def test_a_service_nobody_watches_is_unknown_rather_than_outstanding(self) -> None:
        self.store.record(_task(service="unwatched"))
        observer = RecordingObserver(TaskState.COMPLETED)
        self._poller(observer).tick()
        self.assertEqual(observer.looks, 0)
        self.assertIs(self.store.outstanding()[0].state, TaskState.STATUS_UNKNOWN)

    def test_the_terminal_line_carries_no_finding_text(self) -> None:
        """D-012: identifiers, states and durations only."""
        self.store.record(_task())
        self._poller(RecordingObserver(TaskState.COMPLETED)).tick()
        for _, line in self.lines:
            self.assertNotIn("bug", line.lower())
            self.assertNotIn("severity", line.lower())
            self.assertLess(len(line), 80)


class WatcherCannotActTests(unittest.TestCase):
    """Polling is for visibility. It must be incapable of anything else."""

    MODULES = (
        "src/alx/interfaces/task_poller.py",
        "src/alx/providers/qodo_status.py",
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
        tree = ast.parse(
            (REPOSITORY_ROOT / "src/alx/providers/qodo_status.py").read_text()
        )
        for node in ast.walk(tree):
            # Only calls: `httpx.HTTPError` in an except clause is an
            # attribute too, and catching an error is not making a request.
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                if target.value.id == "httpx":
                    self.assertEqual(target.attr, "get")


class QodoObserverTests(unittest.TestCase):
    """The completion rule Friedl approved, and nothing beyond it."""

    def _observer(self, reviews, comments):
        from alx.providers import qodo_status

        class Response:
            def __init__(self, body):
                self.status_code = 200
                self._body = body

            def json(self):
                return self._body

        def get(url, headers, timeout):
            return Response(reviews if "/reviews" in url else comments)

        original = qodo_status.httpx.get
        qodo_status.httpx.get = get
        self.addCleanup(setattr, qodo_status.httpx, "get", original)
        return QodoStatusObserver("owner/repo", "token")

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
            [
                {
                    "user": {"id": QODO},
                    "body": f"review was updated up to https://github.com/o/r/commit/{HEAD}",
                }
            ],
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
        for subject in ("", "pull/21", f"pull/21@{HEAD[:12]}", "nonsense"):
            with self.subTest(subject=subject):
                self.assertIs(
                    observer.observe(subject).state, TaskState.STATUS_UNKNOWN
                )


if __name__ == "__main__":
    unittest.main()
