"""The PR #77 continuation failure, reproduced and closed.

On 2026-09-27 CodeRabbit began an automatic review of PR #77 and marked its
commit `pending` "Review in progress". Ten seconds later AL/X posted
`@coderabbitai review` anyway. The reviewer's placeholder summary, which names
the head it is working on, was read as the review: the task completed, the
watcher stopped, and the real review with one finding arrived minutes later
with nothing watching for it. Her durable revisit then never fired,
because autonomous cognition was not configured, and she had told Friedl she
would check again at 18:30.

These tests hold each part of that closed through the production path:

- a review round ends only when the reviewer's own exact-head commit status
  says so, so a placeholder settles nothing;
- a round already running or finished on the current head is joined, not
  triggered again;
- a future-cognition receipt says whether anything will act on it;
- a completed review wakes the Core once, into the goal that was waiting.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.autonomous import AutonomousCognitionRunner  # noqa: E402
from alx.bootstrap.review import build_review_runtime  # noqa: E402
from alx.continuity import (  # noqa: E402
    CompletedWorkSource,
    SQLiteContinuityStore,
    SQLiteOpportunityLedger,
)
from alx.continuity.tasks import SQLiteTaskStore  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
    CognitionOrigin,
    ConversationOrigin,
    ConversationTurn,
    GoalMutationKind,
    GoalProposal,
    SuccessCriterion,
    WorkItem,
)
from alx.contracts.continuity import FutureCognitionStatus  # noqa: E402
from alx.contracts.review import ReviewRequest  # noqa: E402
from alx.contracts.review_content import (  # noqa: E402
    REVIEW_FAILED,
    REVIEW_IN_PROGRESS,
    ReviewContentRequest,
)
from alx.contracts.review_provider import ReviewProvider, profile_for  # noqa: E402
from alx.contracts.task import ExternalTask, TaskState  # noqa: E402
from alx.conversation.gateway import ConversationGateway  # noqa: E402
from alx.conversation.store import SQLiteConversationStore  # noqa: E402
from alx.core import CoreAgent  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.interfaces.task_poller import TaskPoller  # noqa: E402
from alx.providers import github_review  # noqa: E402
from alx.providers.github_review import (  # noqa: E402
    NO_REVIEW_FOR_REVISION,
    GitHubReviewProvider,
)
from alx.providers.review_status import (  # noqa: E402
    ReviewStatusObserver,
    subject_reference,
)
from alx.tools.continuity import (  # noqa: E402
    REQUEST_FUTURE_COGNITION,
    build_continuity_executors,
)
from alx.tools.review_content import (  # noqa: E402
    DEFINITION as READ_DEFINITION,
    READ_EXTERNAL_REVIEW,
    build_review_content_executors,
)
from tests.review_transcript import (  # noqa: E402
    BASE,
    HEAD,
    INLINE_BODY,
    OLD_HEAD,
    OTHER_LOGIN,
    REVIEW_ID,
    REVIEWER_LOGIN,
    ROUND_ENDED_AT,
    ROUND_STARTED_AT,
    SUMMARY_BODY,
    SUMMARY_COMMENT_ID,
    ended_round,
    install_grace_clock,
    running_round,
    status,
    transport,
)

NUMBER = 42
REVIEWER = "coderabbit"
NEXT_HEAD = "d" * 40

# What the reviewer posts the moment a round starts, and edits in place later.
# It names the head it is working on, which is exactly why it once passed for
# a review of that head.
PLACEHOLDER_BODY = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "Currently processing new changes in this PR. This may take a few "
    "minutes, please wait...\n\n"
    f"Reviewing files that changed from the base of the PR and between {BASE} "
    f"and {HEAD}.\n"
)
CLEAN_BODY = (
    "No actionable comments were generated in the recent review. 🎉\n\n"
    f"Reviewing files that changed from the base of the PR and between {BASE} "
    f"and {HEAD}.\n"
)

REQUESTED_AT = datetime.fromisoformat(ROUND_STARTED_AT) + timedelta(seconds=10)
# Inside the bounded wait for a task requested at REQUESTED_AT.
WATCHED_AT = REQUESTED_AT + timedelta(minutes=4)


def _user(login: str = REVIEWER_LOGIN) -> dict:
    return {"login": login, "type": "Bot"}


class PullRequest77:
    """GitHub as it looked for one pull request, and as it changes.

    Every list is held by reference in the transport, so moving the review
    from placeholder to finished is a mutation here rather than a new fake.
    """

    def __init__(self) -> None:
        self.comments: list[dict] = []
        self.inline: list[dict] = []
        self.reviews: list[dict] = []
        self.statuses: dict[str, list[dict]] = {}
        self.head = HEAD
        self._install()

    def _install(self) -> None:
        # Rebuilt on a head change, because the transport fixes the head it
        # reports for the pull request when it is created.
        self.request = transport(
            head_sha=self.head,
            number=NUMBER,
            comments=self.comments,
            inline=self.inline,
            submitted=self.reviews,
            statuses=self.statuses,
        )
        github_review.httpx.request = self.request

    @property
    def posted(self) -> list[dict]:
        return self.request.posted  # type: ignore[attr-defined]

    def automatic_review_starts(self, head: str = HEAD) -> None:
        self.statuses[head] = running_round()
        self.comments[:] = [{
            "id": SUMMARY_COMMENT_ID,
            "user": _user(),
            "body": PLACEHOLDER_BODY,
            "created_at": ROUND_STARTED_AT,
            "updated_at": ROUND_STARTED_AT,
        }]

    def review_finishes_with_a_finding(self) -> None:
        self.reviews[:] = [{
            "id": REVIEW_ID,
            "user": _user(),
            "body": SUMMARY_BODY,
            "state": "COMMENTED",
            "commit_id": HEAD,
            "submitted_at": "2026-09-07T06:15:00Z",
        }]
        self.inline[:] = [{
            "id": 8001,
            "user": _user(),
            "body": INLINE_BODY,
            "path": "src/alx/goals/store.py",
            "line": 412,
            "commit_id": HEAD,
            "original_commit_id": HEAD,
            "pull_request_review_id": REVIEW_ID,
        }]
        self.statuses[HEAD] = ended_round()

    def review_finishes_clean(self) -> None:
        self.comments[0] = dict(self.comments[0], body=CLEAN_BODY,
                                updated_at=ROUND_ENDED_AT)
        self.statuses[HEAD] = ended_round()

    def commit_pushed(self, head: str) -> None:
        self.head = head
        self._install()


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        original = github_review.httpx.request
        self.addCleanup(setattr, github_review.httpx, "request", original)
        self.github = PullRequest77()
        self.grace = install_grace_clock(self)
        self.provider = GitHubReviewProvider(
            "owner/repo", "token", profile_for(ReviewProvider.CODERABBIT)
        )
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.store = SQLiteTaskStore(self.root / "tasks.sqlite3")
        self.lines: list[tuple[str, dict]] = []
        self.woken: list[ExternalTask] = []

    def read(self, head: str = HEAD):
        return self.provider.read(ReviewContentRequest(NUMBER, head))

    def observe(self, head: str = HEAD):
        return ReviewStatusObserver(self.provider).observe(
            subject_reference(NUMBER, head), REQUESTED_AT
        ).state

    def poller(self, now: datetime | None = None, maximum: float = 900.0) -> TaskPoller:
        return TaskPoller(
            self.store,
            {REVIEWER: ReviewStatusObserver(self.provider)},
            1.0,
            lambda conversation, values: self.lines.append((conversation, values)),
            self.woken.append,
            clock=(lambda: now) if now is not None else None,
            maximum_wait_seconds=maximum,
        )

    def record_task(self, head: str = HEAD, requested_at: datetime | None = None,
                    task_id: str = "review:42:one") -> None:
        self.store.record(ExternalTask(
            task_id=task_id,
            kind="external_review",
            service=REVIEWER,
            subject_reference=subject_reference(NUMBER, head),
            state=TaskState.REQUESTED,
            requested_at=requested_at or datetime.now(UTC),
            conversation_id="conversation-1",
        ))


class PlaceholderNeverSettlesTests(Harness):
    """Defect 1: only a terminal exact-head signal ends the wait."""

    def test_an_exact_head_placeholder_is_not_a_review(self) -> None:
        self.github.automatic_review_starts()
        content = self.read()
        self.assertFalse(content.available)
        self.assertEqual(content.unavailable_reason, REVIEW_IN_PROGRESS)
        self.assertEqual(content.summary, "")

    def test_an_in_progress_round_keeps_the_task_waiting(self) -> None:
        self.github.automatic_review_starts()
        self.assertIs(self.observe(), TaskState.WAITING_FOR_RESULT)

    def test_the_poller_keeps_watching_while_only_a_placeholder_exists(self) -> None:
        self.github.automatic_review_starts()
        self.record_task()
        poller = self.poller()
        for _ in range(3):
            poller.tick()
        outstanding = self.store.outstanding()
        self.assertEqual([task.task_id for task in outstanding], ["review:42:one"])
        self.assertIs(outstanding[0].state, TaskState.WAITING_FOR_RESULT)
        self.assertEqual(self.woken, [])
        self.assertEqual(self.store.completed_unhandled(), ())

    def test_the_terminal_review_completes_and_its_finding_is_readable(self) -> None:
        """The PR #77 sequence: placeholder, then the review with a finding."""
        self.github.automatic_review_starts()
        self.record_task()
        poller = self.poller()
        poller.tick()
        self.assertEqual(self.woken, [])

        self.github.review_finishes_with_a_finding()
        poller.tick()
        self.assertEqual(self.store.outstanding(), ())
        self.assertEqual([task.task_id for task in self.woken], ["review:42:one"])
        self.assertIs(self.woken[0].state, TaskState.COMPLETED)

        content = self.read()
        self.assertTrue(content.available)
        self.assertEqual(content.head_sha, HEAD)
        self.assertEqual([item.body for item in content.comments], [INLINE_BODY])
        self.assertEqual(content.comments[0].path, "src/alx/goals/store.py")

    def test_a_clean_terminal_review_completes(self) -> None:
        self.github.automatic_review_starts()
        self.assertIs(self.observe(), TaskState.WAITING_FOR_RESULT)
        self.github.review_finishes_clean()
        self.assertIs(self.observe(), TaskState.COMPLETED)
        content = self.read()
        self.assertTrue(content.available)
        self.assertEqual(content.summary, CLEAN_BODY)
        self.assertEqual(content.comments, ())

    def test_a_finished_review_of_another_head_does_not_complete_this_one(self) -> None:
        self.github.statuses[OLD_HEAD] = ended_round()
        self.github.comments.append({
            "id": 7000, "user": _user(),
            "body": f"No actionable comments. Between {BASE} and {OLD_HEAD}.",
            "created_at": ROUND_ENDED_AT,
        })
        self.github.reviews.append({
            "id": 5000, "user": _user(), "body": "Old round.",
            "state": "COMMENTED", "commit_id": OLD_HEAD,
            "submitted_at": ROUND_ENDED_AT,
        })
        self.github.statuses[HEAD] = running_round()
        self.assertIs(self.observe(HEAD), TaskState.WAITING_FOR_RESULT)
        self.assertTrue(self.read(OLD_HEAD).available)

    def test_no_status_at_all_is_still_no_review(self) -> None:
        """A summary naming the head is not enough without the round ending."""
        self.github.comments.append({
            "id": 1, "user": _user(), "body": SUMMARY_BODY,
            "created_at": ROUND_ENDED_AT,
        })
        content = self.read()
        self.assertFalse(content.available)
        self.assertEqual(content.unavailable_reason, NO_REVIEW_FOR_REVISION)
        self.assertIs(self.observe(), TaskState.WAITING_FOR_RESULT)

    def test_only_the_reviewer_s_own_status_context_ends_a_round(self) -> None:
        self.github.automatic_review_starts()
        for impostor in (
            status("success", "2026-09-07T06:20:00Z", login=OTHER_LOGIN),
            status("success", "2026-09-07T06:20:00Z", login="coderabbitai-evil[bot]"),
            status("success", "2026-09-07T06:20:00Z", context="law-gates"),
        ):
            with self.subTest(impostor=impostor):
                self.github.statuses[HEAD] = [impostor, *running_round()]
                self.assertIs(self.observe(), TaskState.WAITING_FOR_RESULT)

    def test_a_new_round_on_the_same_head_reopens_the_wait(self) -> None:
        """The latest status decides; a finished earlier round does not."""
        self.github.statuses[HEAD] = [
            status("pending", "2026-09-07T06:30:00Z"),
            *ended_round(),
        ]
        self.github.comments.append({
            "id": 1, "user": _user(), "body": SUMMARY_BODY,
            "created_at": ROUND_ENDED_AT,
        })
        self.assertIs(self.observe(), TaskState.WAITING_FOR_RESULT)

    def test_a_failed_round_is_surfaced_as_a_failure(self) -> None:
        self.github.automatic_review_starts()
        self.github.statuses[HEAD] = [
            status("failure", "2026-09-07T06:14:00Z"), *running_round(),
        ]
        content = self.read()
        self.assertFalse(content.available)
        self.assertEqual(content.unavailable_reason, REVIEW_FAILED)
        self.assertIs(self.observe(), TaskState.FAILED)

        self.record_task()
        self.poller().tick()
        self.assertEqual(self.store.outstanding(), ())
        self.assertIs(self.woken[-1].state, TaskState.FAILED)

    def test_waiting_on_a_placeholder_is_still_bounded(self) -> None:
        self.github.automatic_review_starts()
        asked = datetime(2026, 9, 7, 6, 12, 10, tzinfo=UTC)
        self.record_task(requested_at=asked)
        poller = self.poller(now=asked + timedelta(seconds=899), maximum=900.0)
        poller.tick()
        self.assertEqual(len(self.store.outstanding()), 1)

        poller = self.poller(now=asked + timedelta(seconds=900), maximum=900.0)
        poller.tick()
        self.assertEqual(self.store.outstanding(), ())
        self.assertIs(self.woken[-1].state, TaskState.FAILED)


class NoRedundantTriggerTests(Harness):
    """Defect 3: a round already on the exact head is joined, not re-asked."""

    def request(self):
        return self.provider.request(ReviewRequest(pull_request_number=NUMBER))

    def test_a_pending_automatic_review_is_not_triggered_again(self) -> None:
        self.github.statuses[HEAD] = running_round()
        outcome = self.request()
        self.assertEqual(self.github.posted, [])
        self.assertFalse(outcome.requested)
        self.assertEqual(outcome.head_sha, HEAD)
        self.assertIsNotNone(outcome.requested_at)

    def test_an_in_progress_automatic_review_is_not_triggered_again(self) -> None:
        self.github.automatic_review_starts()
        outcome = self.request()
        self.assertEqual(self.github.posted, [])
        self.assertEqual(outcome.head_sha, HEAD)

    def test_a_finished_review_of_this_head_is_not_triggered_again(self) -> None:
        self.github.statuses[HEAD] = ended_round()
        outcome = self.request()
        self.assertEqual(self.github.posted, [])
        self.assertFalse(outcome.requested)

    def test_with_no_review_the_trigger_is_still_posted(self) -> None:
        outcome = self.request()
        self.assertEqual(self.github.posted, [{"body": "@coderabbitai review"}])
        self.assertTrue(outcome.requested)
        self.assertEqual(outcome.head_sha, HEAD)

    def test_a_review_of_another_head_does_not_suppress_the_trigger(self) -> None:
        self.github.statuses[OLD_HEAD] = ended_round()
        outcome = self.request()
        self.assertEqual(len(self.github.posted), 1)
        self.assertEqual(outcome.head_sha, HEAD)

    def test_a_failed_round_can_still_be_asked_for_again(self) -> None:
        self.github.statuses[HEAD] = [status("error", ROUND_ENDED_AT), *running_round()]
        outcome = self.request()
        self.assertEqual(len(self.github.posted), 1)
        self.assertTrue(outcome.requested)

    def test_a_new_commit_needs_its_own_review(self) -> None:
        """The head moved: the old head's round says nothing about the new one."""
        self.github.statuses[HEAD] = ended_round()
        self.assertFalse(self.request().requested)

        self.github.commit_pushed(NEXT_HEAD)
        outcome = self.request()
        self.assertEqual(len(self.github.posted), 1)
        self.assertEqual(outcome.head_sha, NEXT_HEAD)

    def test_a_new_commit_the_reviewer_already_picked_up_is_joined(self) -> None:
        self.github.commit_pushed(NEXT_HEAD)
        self.github.statuses[NEXT_HEAD] = running_round()
        outcome = self.request()
        self.assertEqual(self.github.posted, [])
        self.assertEqual(outcome.head_sha, NEXT_HEAD)

    def test_the_capability_attaches_its_wait_to_the_existing_round(self) -> None:
        """Through the production composition: no trigger, same waiter."""
        self.github.statuses[HEAD] = running_round()
        waits: list[tuple] = []

        def started(number, head_sha, requested_at):
            waits.append((number, head_sha))
            return "completed"

        runtime = build_review_runtime(
            True, "owner/repo", "token", lambda: "call-1", started=started
        )
        result = runtime.executors["request_external_review"](
            {"pull_request_number": NUMBER}
        )
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(self.github.posted, [])
        self.assertEqual(waits, [(NUMBER, HEAD)])
        self.assertFalse(result.values["requested"])
        self.assertEqual(result.values["head_sha"], HEAD)
        self.assertEqual(result.values["wait_state"], "completed")


class AutomaticReviewGraceTests(Harness):
    """The race after PR #77: asked before the reviewer has marked the head.

    CodeRabbit marks a new head `pending` seconds after it appears. A request
    in that window must wait a bounded grace for the mark, mechanically and
    inside the one call, and trigger only if none comes.
    """

    def request(self):
        return self.provider.request(ReviewRequest(pull_request_number=NUMBER))

    def publish_after(self, sleeps: int, head: str, statuses) -> None:
        def on_sleep(count):
            if count == sleeps:
                self.github.statuses[head] = statuses
        self.grace = install_grace_clock(self, on_sleep)

    def test_a_new_pr_marked_pending_during_the_grace_is_joined(self) -> None:
        self.publish_after(1, HEAD, running_round())
        outcome = self.request()
        self.assertEqual(self.github.posted, [])
        self.assertFalse(outcome.requested)
        self.assertEqual(outcome.head_sha, HEAD)
        self.assertEqual(self.grace.sleeps, [10.0])

    def test_a_new_head_marked_pending_during_the_grace_is_joined(self) -> None:
        self.github.statuses[HEAD] = ended_round()
        self.github.commit_pushed(NEXT_HEAD)
        self.publish_after(2, NEXT_HEAD, running_round())
        outcome = self.request()
        self.assertEqual(self.github.posted, [])
        self.assertEqual(outcome.head_sha, NEXT_HEAD)

    def test_a_round_that_finishes_during_the_grace_is_joined(self) -> None:
        self.publish_after(3, HEAD, ended_round())
        outcome = self.request()
        self.assertEqual(self.github.posted, [])
        self.assertFalse(outcome.requested)

    def test_no_automatic_review_by_the_end_of_the_grace_gets_one_trigger(self) -> None:
        outcome = self.request()
        self.assertEqual(self.github.posted, [{"body": "@coderabbitai review"}])
        self.assertTrue(outcome.requested)
        self.assertEqual(outcome.head_sha, HEAD)
        # Bounded: sixty seconds, rechecked every ten, then exactly one ask.
        self.assertEqual(sum(self.grace.sleeps), 60.0)
        self.assertEqual(set(self.grace.sleeps), {10.0})

    def test_the_old_head_s_review_does_not_stand_in_for_the_new_one(self) -> None:
        self.github.statuses[HEAD] = ended_round()
        self.github.commit_pushed(NEXT_HEAD)
        outcome = self.request()
        self.assertEqual(len(self.github.posted), 1)
        self.assertEqual(outcome.head_sha, NEXT_HEAD)

    def test_a_push_during_the_grace_is_followed_to_the_new_head(self) -> None:
        def on_sleep(count):
            if count == 1:
                self.github.commit_pushed(NEXT_HEAD)
            if count == 2:
                self.github.statuses[NEXT_HEAD] = running_round()
        self.grace = install_grace_clock(self, on_sleep)
        outcome = self.request()
        self.assertEqual(self.github.posted, [])
        self.assertEqual(outcome.head_sha, NEXT_HEAD)

    def test_a_failed_round_is_asked_again_without_waiting(self) -> None:
        self.github.statuses[HEAD] = [status("failure", ROUND_ENDED_AT)]
        self.request()
        self.assertEqual(len(self.github.posted), 1)
        self.assertEqual(self.grace.sleeps, [])

    def test_the_pr_77_sequence_posts_no_trigger(self) -> None:
        """Opened, asked at once, marked `pending` seconds later: no trigger.

        On PR #77 AL/X asked ten seconds after the automatic round began; here
        she asks before it begins, the harder case, and the capability still
        attaches its one wait to the automatic round.
        """
        self.publish_after(1, HEAD, running_round())
        waits: list[tuple] = []

        def started(number, head_sha, requested_at):
            waits.append((number, head_sha))
            return "completed"

        runtime = build_review_runtime(
            True, "owner/repo", "token", lambda: "call-1", started=started
        )
        result = runtime.executors["request_external_review"](
            {"pull_request_number": NUMBER}
        )
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(self.github.posted, [])
        self.assertFalse(result.values["requested"])
        self.assertEqual(waits, [(NUMBER, HEAD)])

    def test_repeated_requests_never_trigger_twice_for_one_head(self) -> None:
        self.request()
        self.github.statuses[HEAD] = running_round()
        self.request()
        self.request()
        self.assertEqual(len(self.github.posted), 1)

    def test_the_grace_is_mechanical(self) -> None:
        """No Core, no scheduler: GitHub reads and a sleep inside one call."""
        import ast

        tree = ast.parse((
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "providers" / "github_review.py"
        ).read_text(encoding="utf-8"))
        imported = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertFalse(any(
            name.startswith(("alx.core", "alx.continuity", "alx.interfaces",
                             "alx.bootstrap", "asyncio"))
            for name in imported
        ))
        seen: list[str] = []
        original = self.github.request

        def recording(method, url, **keywords):
            seen.append(method)
            return original(method, url, **keywords)

        github_review.httpx.request = recording
        self.request()
        # Every call during the grace was a read; the one write is the trigger.
        self.assertEqual(seen.count("POST"), 1)
        self.assertEqual(seen[-2], "POST")
        self.assertTrue(all(method == "GET" for method in seen[:-2]))


class UnserviceableContinuationTests(unittest.TestCase):
    """Defect 2, where autonomy is off: say so, and keep the work."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteContinuityStore(Path(directory.name) / "c.sqlite3")
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 9, 27, 18, 24, tzinfo=UTC)

    def _request(self, available: bool):
        executors = build_continuity_executors(
            self.store, 30, lambda: "call-1", clock=lambda: self.now,
            conversation_id_source=lambda: "conversation-1",
            autonomous_available=available,
        )
        return executors[REQUEST_FUTURE_COGNITION]({
            "request_id": f"revisit-{available}",
            "not_before": (self.now + timedelta(minutes=6)).isoformat(),
            "note": "check the review",
        })

    def test_an_unserviceable_request_says_so(self) -> None:
        result = self._request(False)
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["autonomous_cognition"], "unavailable")

    def test_an_unserviceable_request_is_still_kept(self) -> None:
        self._request(False)
        pending = self.store.pending()
        self.assertEqual([item.request_id for item in pending], ["revisit-False"])
        self.assertIs(pending[0].status, FutureCognitionStatus.PENDING)

    def test_a_serviceable_request_says_so(self) -> None:
        self.assertEqual(self._request(True).values["autonomous_cognition"], "available")

    def test_composition_reports_the_same_switch_every_source_uses(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "autonomous_available=providers.autonomous is not None", source
        )
        self.assertEqual(source.count("autonomous_available="), 1)

    def test_a_completion_nothing_can_service_is_kept_for_later(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        tasks = SQLiteTaskStore(root / "tasks.sqlite3")
        tasks.record(ExternalTask(
            task_id="review:42:one", kind="external_review", service=REVIEWER,
            subject_reference=subject_reference(NUMBER, HEAD),
            state=TaskState.COMPLETED, requested_at=self.now,
            completed_at=self.now, conversation_id="conversation-1",
        ))
        ledger = SQLiteOpportunityLedger(root / "o.sqlite3")
        self.addCleanup(ledger.close)
        self.assertEqual(
            CompletedWorkSource(tasks, ledger, enabled=False).due_opportunities(), ()
        )
        self.assertEqual(len(tasks.completed_unhandled()), 1)
        # Enabled later, in a new process, it is offered exactly once.
        reopened = SQLiteTaskStore(root / "tasks.sqlite3")
        offered = CompletedWorkSource(reopened, ledger, enabled=True).due_opportunities()
        self.assertEqual(
            [item.opportunity_id for item in offered], ["task:review:42:one"]
        )


class Reasoner:
    """Scripted decisions, recording every context the Core was given."""

    def __init__(self) -> None:
        self.contexts = []
        self.read = False

    def decide(self, context):
        self.contexts.append(context)
        if context.origin is CognitionOrigin.PERSON_TURN:
            return AgentDecision(
                response="PR 42 is open; I'll judge the review when it lands.",
                goal_proposal=GoalProposal(
                    GoalMutationKind.CREATE,
                    "Land PR 42 once its external review is judged",
                    (SuccessCriterion("review-judged", "the review is judged"),),
                    blockers=(WorkItem("external-review", "awaiting review of PR 42"),),
                    outstanding_work=(
                        WorkItem("judge-review", "judge the external review findings"),
                    ),
                ),
            )
        if context.active_goal is None:
            # She chooses which unfinished goal the occasion belongs to. The
            # script stands in for that judgement; the Core only offers them.
            return AgentDecision(goal_id=context.unfinished_goals[0].goal_id)
        if not self.read:
            self.read = True
            # The review it was blocked on has arrived, so she resumes the goal
            # at its recorded stage — the blocker clears, the outstanding
            # judgement stays — and reads what the reviewer said.
            return AgentDecision(
                call=CapabilityCall(
                    "read-1", READ_EXTERNAL_REVIEW,
                    {"pull_request_number": NUMBER, "head_sha": HEAD},
                ),
                goal_id=context.active_goal.goal_id,
                goal_proposal=GoalProposal(GoalMutationKind.UPDATE, blockers=()),
            )
        return AgentDecision(
            response="The review has one finding to judge.",
            goal_id=context.active_goal.goal_id,
        )


class DurableReviewContinuationTests(Harness):
    """PR opened -> placeholder -> restart -> terminal review -> one wake."""

    def test_the_whole_continuation(self) -> None:
        conversations = SQLiteConversationStore(self.root / "conversations.sqlite3")
        self.addCleanup(conversations.close)
        goals = SQLiteGoalStore(self.root / "goals.sqlite3")
        self.addCleanup(goals.close)
        ledger = SQLiteOpportunityLedger(self.root / "opportunities.sqlite3")
        self.addCleanup(ledger.close)

        read = build_review_content_executors(self.provider.read, lambda: "read-1")
        attempts: list[CapabilityAttempt] = []

        def dispatch(call, state):
            attempt = CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                read[call.capability_id](call.arguments),
            )
            attempts.append(attempt)
            return attempt

        reasoner = Reasoner()
        clock = lambda: datetime(2026, 9, 7, 6, 30, tzinfo=UTC)  # noqa: E731
        identifiers = iter(f"id-{index}" for index in range(100))
        core = CoreAgent(
            goals, reasoner, dispatch, (READ_DEFINITION,), clock=clock,
            identifier_factory=lambda: next(identifiers),
            approval_free_capabilities=frozenset({READ_EXTERNAL_REVIEW}),
        )
        gateway = ConversationGateway(
            core, conversations, identifier_factory=lambda: next(identifiers),
            clock=clock,
        )
        retention = datetime(2027, 9, 7, tzinfo=UTC)

        # The CA goal is blocked on the external review.
        gateway.receive_conversation_turn(
            ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                             "open the PR and see it through", clock()),
            8, retention,
        )
        blocked = [goal.state.goal_id for goal in goals.list_goals()]
        self.assertEqual(len(blocked), 1)

        # PR opened; the reviewer starts on its own and posts a placeholder.
        self.github.automatic_review_starts()
        self.record_task(requested_at=REQUESTED_AT)
        self.poller(now=WATCHED_AT).tick()
        self.assertEqual(self.woken, [])
        self.assertEqual(self.store.completed_unhandled(), ())

        # The process restarts while the review is still running.
        self.store = SQLiteTaskStore(self.root / "tasks.sqlite3")
        self.assertEqual(len(self.store.outstanding()), 1)
        self.poller(now=WATCHED_AT).tick()
        self.assertEqual(self.store.completed_unhandled(), ())

        # The real review lands, with a finding.
        self.github.review_finishes_with_a_finding()
        self.poller(now=WATCHED_AT).tick()
        self.assertEqual(len(self.woken), 1)

        wakes: list[str] = []
        receive = gateway.receive_cognition_opportunity

        def counted(conversation_id, opportunity, *rest):
            wakes.append(opportunity.opportunity_id)
            return receive(conversation_id, opportunity, *rest)

        gateway.receive_cognition_opportunity = counted  # type: ignore[method-assign]
        source = CompletedWorkSource(self.store, ledger, enabled=True)
        runner = AutonomousCognitionRunner(
            source, ledger, gateway, 8, 365, clock=clock,
        )
        due = source.due_opportunities()
        self.assertEqual([item.origin for item in due], [CognitionOrigin.WORK_COMPLETED])
        self.assertTrue(runner.run_one(due[0]))

        # One occasion, one Core turn, in the conversation the work was for.
        self.assertEqual(wakes, ["task:review:42:one"])
        woken = [c for c in reasoner.contexts
                 if c.origin is CognitionOrigin.WORK_COMPLETED]
        self.assertTrue(woken)
        self.assertEqual({c.conversation_id for c in woken}, {"conversation-1"})
        # The same blocked goal, carrying its recorded stage, is what she wakes
        # into: nothing restarts it and nothing replaces it.
        summaries = {item.goal_id: item for item in woken[0].unfinished_goals}
        self.assertEqual(set(summaries), set(blocked))
        self.assertIn("awaiting review of PR 42", summaries[blocked[0]].blockers)
        self.assertEqual([goal.state.goal_id for goal in goals.list_goals()], blocked)
        resumed = goals.list_goals()[0].state
        self.assertEqual(resumed.blockers, ())
        self.assertEqual(
            [item.item_id for item in resumed.outstanding_work], ["judge-review"]
        )
        # The finding reached her from the terminal review.
        self.assertEqual(len(attempts), 1)
        values = attempts[0].result.values
        self.assertTrue(values["available"])
        self.assertEqual(values["comments"][0]["body"], INLINE_BODY)

        # Consumed exactly once: not offered again, not runnable again, and
        # still not after another restart.
        self.assertEqual(source.due_opportunities(), ())
        self.assertFalse(runner.run_one(due[0]))
        reopened = CompletedWorkSource(
            SQLiteTaskStore(self.root / "tasks.sqlite3"), ledger, enabled=True
        )
        self.assertEqual(reopened.due_opportunities(), ())
        self.assertEqual(wakes, ["task:review:42:one"])


if __name__ == "__main__":
    unittest.main()
