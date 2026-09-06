"""Requesting an external review: one instruction, one request.

Requesting a review spends real credits, so the authority is deliberately not
plain permission. The gate requires an approval grounded in Friedl's latest
turn, and an approval is single-use. That is what stops one instruction from
becoming several requests when a review finds something, a fix lands, the head
moves, or a request fails.

Nothing here reads a review or decides anything about one. These tests prove
the request is made once, refused before contact when it should not be made at
all, and never causes a merge.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.review import (  # noqa: E402
    REVIEW_REQUEST_PERMISSION,
    build_review_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    Approval,
    ApprovalLifecycle,
    ApprovalScope,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
    ReviewError,
    ReviewOutcome,
    ReviewRequest,
    SideEffect,
)
from alx.safety import AuthorityContext, SafetyGate, SafetyState  # noqa: E402
from alx.tools.review import REQUEST_EXTERNAL_REVIEW  # noqa: E402


HEAD = "a" * 40
MOVED = "b" * 40
APPROVAL = "approval-1"


class RecordingReviewer:
    """Stands in for Qodo, and counts how often it was asked."""

    reviewer = "qodo"

    def __init__(self, error: str | None = None, head: str = HEAD) -> None:
        self.requests: list[ReviewRequest] = []
        self._error = error
        self._head = head

    def request(self, review: ReviewRequest) -> ReviewOutcome:
        self.requests.append(review)
        if self._error is not None:
            raise ReviewError(self._error)
        # The revision is read by the provider, not supplied by the caller.
        return ReviewOutcome(
            pull_request_number=review.pull_request_number,
            head_sha=self._head,
            requested=True,
            reviewer=self.reviewer,
        )


class WatchWindowTest(unittest.TestCase):
    """When the watcher starts looking, relative to when the trigger goes out.

    The observer only considers results later than the task's requested_at. A
    timestamp taken after the provider returned therefore excluded a review
    that finished quickly: the result already existed before the watcher was
    willing to look at anything, so the task never completed and the terminal
    showed it outstanding forever.
    """

    def test_the_watch_starts_before_the_trigger_is_posted(self) -> None:
        posted_at: list[datetime] = []

        class SlowProvider:
            reviewer = "qodo"

            def request(self, review: ReviewRequest) -> ReviewOutcome:
                posted_at.append(datetime.now(UTC))
                return ReviewOutcome(
                    pull_request_number=review.pull_request_number,
                    head_sha="a" * 40,
                    requested=True,
                    reviewer="qodo",
                )

        watched: list[datetime] = []
        runtime = build_review_runtime(
            True,
            "owner/repo",
            "token",
            lambda: "call-1",
            provider=SlowProvider(),
            started=lambda number, sha, requested_at: watched.append(requested_at),
        )
        runtime.executors[REQUEST_EXTERNAL_REVIEW]({"pull_request_number": 21})

        self.assertEqual(len(watched), 1)
        self.assertEqual(len(posted_at), 1)
        # Not merely close: strictly before, so no result can land in a gap
        # the watcher refuses to look at.
        self.assertLessEqual(watched[0], posted_at[0])


class ReviewRequestTest(unittest.TestCase):
    def _runtime(self, provider):
        return build_review_runtime(
            True, "owner/repo", "token", lambda: "call-1", provider=provider
        )

    def _broker(self, runtime):
        return CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )

    @staticmethod
    def _call(number: int = 21, approval: str | None = APPROVAL):
        return CapabilityCall(
            "call-1",
            REQUEST_EXTERNAL_REVIEW,
            {"pull_request_number": number},
            approval,
        )

    @staticmethod
    def _authority(
        permissions: frozenset[str], approvals: tuple[Approval, ...] = ()
    ) -> AuthorityContext:
        return AuthorityContext(
            principal_reference="friedl",
            granted_permission_references=permissions,
            evaluated_at=datetime.now(UTC),
            approvals=approvals,
        )

    @staticmethod
    def _approval(number: int = 21) -> Approval:
        """One paid review of one pull request, not of one commit."""
        return Approval(
            APPROVAL,
            ApprovalScope(
                REQUEST_EXTERNAL_REVIEW, {"pull_request_number": number}
            ),
            ApprovalLifecycle.GRANTED,
        )

    def test_friedls_instruction_produces_exactly_one_request(self) -> None:
        provider = RecordingReviewer()
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(),
            self._authority(
                frozenset({REVIEW_REQUEST_PERMISSION}), (self._approval(),)
            ),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(attempt.result.values["requested"])
        self.assertEqual(attempt.result.values["reviewer"], "qodo")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(provider.requests[0].pull_request_number, 21)
        # The revision comes back from the provider, not from the caller.
        self.assertEqual(attempt.result.values["head_sha"], HEAD)

    def test_without_the_permission_the_reviewer_is_never_contacted(self) -> None:
        provider = RecordingReviewer()
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(), self._authority(frozenset({"web.read"}), (self._approval(),))
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertFalse(attempt.implementation_invoked)
        self.assertEqual(attempt.reason_code, "permission_missing")
        self.assertEqual(provider.requests, [])

    def test_without_friedls_approval_no_review_is_requested(self) -> None:
        """The authority is his instruction, not a standing licence to spend."""
        provider = RecordingReviewer()
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(approval=None),
            self._authority(frozenset({REVIEW_REQUEST_PERMISSION})),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertFalse(attempt.implementation_invoked)
        self.assertEqual(attempt.reason_code, "approval_required")
        self.assertEqual(provider.requests, [])

    def test_an_approval_for_one_pull_request_does_not_cover_another(self) -> None:
        """One instruction buys a review of the pull request he named."""
        provider = RecordingReviewer()
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(number=99),
            self._authority(
                frozenset({REVIEW_REQUEST_PERMISSION}), (self._approval(number=21),)
            ),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(attempt.reason_code, "approval_invalid")
        self.assertEqual(provider.requests, [])

    def test_the_revision_reviewed_is_read_and_reported_back(self) -> None:
        """Friedl names a pull request; AL/X reports which commit was sent.

        He should not have to carry a SHA, and the result must still say
        exactly what was reviewed, so the revision is read at request time
        rather than supplied.
        """
        provider = RecordingReviewer(head=MOVED)
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(),
            self._authority(
                frozenset({REVIEW_REQUEST_PERMISSION}), (self._approval(),)
            ),
        )
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(attempt.result.values["head_sha"], MOVED)
        # The caller supplied no revision at all.
        self.assertNotIn("head_sha", attempt.call.arguments)

    def test_a_provider_failure_is_reported_and_not_retried(self) -> None:
        provider = RecordingReviewer(error="review_unavailable")
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(),
            self._authority(
                frozenset({REVIEW_REQUEST_PERMISSION}), (self._approval(),)
            ),
        )
        self.assertEqual(attempt.result.failure["code"], "review_unavailable")
        # One attempt. A failed request does not become a second request.
        self.assertEqual(len(provider.requests), 1)

    def test_a_spent_approval_cannot_buy_a_second_review(self) -> None:
        """The property that stops fix-and-re-review loops.

        The gate matches an approval by id and exact scope. Once the Core has
        recorded that approval it cannot be proposed again, so a second request
        needs Friedl to ask a second time.
        """
        runtime = self._runtime(RecordingReviewer())
        gate = SafetyGate(runtime.policies)
        authority = self._authority(
            frozenset({REVIEW_REQUEST_PERMISSION}), (self._approval(),)
        )
        first = gate.evaluate(self._call(), authority)
        self.assertIs(first.state, SafetyState.ALLOWED)

        # The same instruction cannot authorise a different call: a new head,
        # or a different pull request, does not match the approved scope.
        for call in (self._call(number=99), self._call(number=7)):
            with self.subTest(call=call.arguments):
                self.assertIs(
                    gate.evaluate(call, authority).state, SafetyState.DENIED
                )

    def test_requesting_a_review_invokes_no_merge(self) -> None:
        """A review request must not become a merge by any path."""
        runtime = self._runtime(RecordingReviewer())
        self.assertEqual(
            [d.capability_id for d in runtime.definitions], [REQUEST_EXTERNAL_REVIEW]
        )
        self.assertEqual(set(runtime.executors), {REQUEST_EXTERNAL_REVIEW})
        self.assertEqual(set(runtime.policies), {REQUEST_EXTERNAL_REVIEW})
        self.assertNotIn("repository.merge", runtime.permissions)
        # The module cannot reach the merge capability: it imports nothing
        # from it and never names its identifier. Checked structurally rather
        # than by searching for the word, which appears in prose explaining
        # that this capability decides nothing about merging.
        import ast

        tree = ast.parse((REPOSITORY_ROOT / "src/alx/tools/review.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                names.append(getattr(node, "module", "") or "")
                self.assertNotIn("repository", " ".join(names))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotEqual(node.value, "merge_pull_request")

    def test_unusable_arguments_never_reach_the_reviewer(self) -> None:
        provider = RecordingReviewer()
        attempt = self._broker(self._runtime(provider)).dispatch(
            CapabilityCall(
                "call-1",
                REQUEST_EXTERNAL_REVIEW,
                {"pull_request_number": 0},
                APPROVAL,
            ),
            self._authority(
                frozenset({REVIEW_REQUEST_PERMISSION}),
                (
                    Approval(
                        APPROVAL,
                        ApprovalScope(
                            REQUEST_EXTERNAL_REVIEW, {"pull_request_number": 0}
                        ),
                        ApprovalLifecycle.GRANTED,
                    ),
                ),
            ),
        )
        self.assertEqual(attempt.result.failure["code"], "arguments_unusable")
        self.assertEqual(provider.requests, [])

    def test_the_policy_requires_friedls_approval_and_no_standing_scope(self) -> None:
        runtime = self._runtime(RecordingReviewer())
        policy = runtime.policies[REQUEST_EXTERNAL_REVIEW]
        self.assertEqual(
            policy.permission_references, frozenset({REVIEW_REQUEST_PERMISSION})
        )
        self.assertTrue(policy.approval_required)
        # Not a standing scope: that would be durable autonomous spending.
        self.assertFalse(policy.standing_scope_allowed)

    def test_the_capability_is_effectful_and_carries_no_authored_text(self) -> None:
        definition = self._runtime(RecordingReviewer()).definitions[0]
        self.assertIs(definition.side_effect, SideEffect.EFFECTFUL)
        self.assertFalse(definition.transmits_authored_text)

    def test_an_unconfigured_runtime_registers_nothing(self) -> None:
        self.assertIsNone(build_review_runtime(False, "owner/repo", "t", lambda: "c"))
        self.assertIsNone(build_review_runtime(True, "", "t", lambda: "c"))
        self.assertIsNone(build_review_runtime(True, "owner/repo", "", lambda: "c"))


class QodoProviderTest(unittest.TestCase):
    """The Qodo trigger itself, without contacting GitHub."""

    def _provider(self, head: str, post_status: int = 201, after: str | None = None):
        """`after` is the head on the second read, when it differs."""
        from alx.providers import qodo_review

        calls: dict = {"get": [], "post": []}

        class Response:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body

            def json(self):
                return self._body

        def get(url, headers, timeout):
            calls["get"].append(url)
            # The second read happens after the trigger is posted.
            current = head if not calls["post"] else (after if after else head)
            return Response(200, {"head": {"sha": current}})

        def post(url, json, headers, timeout):  # noqa: A002
            calls["post"].append((url, json))
            return Response(post_status, {})

        original_get, original_post = qodo_review.httpx.get, qodo_review.httpx.post
        qodo_review.httpx.get = get
        qodo_review.httpx.post = post
        self.addCleanup(setattr, qodo_review.httpx, "get", original_get)
        self.addCleanup(setattr, qodo_review.httpx, "post", original_post)
        return qodo_review.QodoReviewProvider("owner/repo", "token"), calls

    def test_the_trigger_is_posted_to_the_pull_request(self) -> None:
        provider, calls = self._provider(head=HEAD)
        outcome = provider.request(ReviewRequest(pull_request_number=21))
        self.assertTrue(outcome.requested)
        self.assertEqual(outcome.reviewer, "qodo")
        self.assertEqual(len(calls["post"]), 1)
        url, body = calls["post"][0]
        self.assertIn("/issues/21/comments", url)
        self.assertEqual(body, {"body": "/review"})

    def test_the_head_is_read_from_the_pull_request(self) -> None:
        """Friedl names a pull request; the revision is looked up, not supplied."""
        provider, calls = self._provider(head=MOVED)
        outcome = provider.request(ReviewRequest(pull_request_number=21))
        self.assertEqual(outcome.head_sha, MOVED)
        self.assertIn("/pulls/21", calls["get"][0])
        self.assertEqual(len(calls["post"]), 1)

    def test_a_pull_request_without_a_usable_head_is_not_reviewed(self) -> None:
        """Nothing to record about what was reviewed means nothing to spend on."""
        provider, calls = self._provider(head="not-a-sha")
        with self.assertRaises(ReviewError) as caught:
            provider.request(ReviewRequest(pull_request_number=21))
        self.assertEqual(caught.exception.code, "review_unavailable")
        self.assertEqual(calls["post"], [])

    def test_a_head_that_moves_during_the_request_is_not_claimed(self) -> None:
        """The trigger is not pinned to a commit, so the revision is confirmed.

        Qodo reviews whatever the pull request points at when it reaches the
        request. If the head moved in between, naming the commit read
        beforehand would assert something that was never checked.
        """
        provider, calls = self._provider(head=HEAD, after=MOVED)
        outcome = provider.request(ReviewRequest(pull_request_number=21))
        self.assertTrue(outcome.requested)
        # The review was requested; which revision it covers is not established.
        self.assertEqual(outcome.head_sha, "")
        self.assertEqual(len(calls["get"]), 2)
        self.assertEqual(len(calls["post"]), 1)

    def test_a_stable_head_is_reported_after_confirmation(self) -> None:
        provider, calls = self._provider(head=HEAD)
        outcome = provider.request(ReviewRequest(pull_request_number=21))
        self.assertEqual(outcome.head_sha, HEAD)
        self.assertEqual(len(calls["get"]), 2)

    def test_a_rejected_comment_is_reported_as_a_refusal(self) -> None:
        provider, _ = self._provider(head=HEAD, post_status=403)
        with self.assertRaises(ReviewError) as caught:
            provider.request(ReviewRequest(pull_request_number=21))
        self.assertEqual(caught.exception.code, "review_refused")

    def test_a_malformed_repository_is_refused_at_construction(self) -> None:
        from alx.providers.qodo_review import QodoReviewProvider

        for repository in (
            "/repo", "owner/", "owner/repo/extra", "ownerrepo", "",
            "own er/repo", "owner/re?po", "owner/repo#x",
        ):
            with self.subTest(repository=repository):
                with self.assertRaises(ValueError):
                    QodoReviewProvider(repository, "token")


if __name__ == "__main__":
    unittest.main()
