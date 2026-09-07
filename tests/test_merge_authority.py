"""Friedl's delegated merge authority, and the two ways it must fail closed.

He delegated routine merge authorisation to AL/X rather than approving each
merge. So the permission is the authority: holding it lets her merge, and
revoking it stops her. There is no per-merge approval to obtain, and these
tests assert that absence as deliberately as they assert the refusals.

Two refusals matter most. Without `repository.merge` the executor is never
reached, so a runtime that was never given the authority cannot merge by
accident. And a merge names the exact revision that was reviewed, so a head
that moved after AL/X judged it is refused rather than merged unreviewed.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.repository import (  # noqa: E402
    REPOSITORY_MERGE_PERMISSION,
    build_repository_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
    MergeError,
    MergeOutcome,
    MergeRequest,
    SideEffect,
)
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.repository import MERGE_PULL_REQUEST  # noqa: E402


HEAD = "a" * 40
OTHER = "b" * 40


class RecordingProvider:
    """Stands in for GitHub, and records what it was asked to merge."""

    def __init__(self, outcome=None, error: str | None = None) -> None:
        self.requests: list[MergeRequest] = []
        self._outcome = outcome
        self._error = error

    def merge(self, request: MergeRequest):
        self.requests.append(request)
        if self._error is not None:
            raise MergeError(self._error)
        return self._outcome or MergeOutcome(
            pull_request_number=request.pull_request_number,
            head_sha=request.head_sha,
            merged=True,
            merge_commit_sha="c" * 40,
        )


class MergeAuthorityTest(unittest.TestCase):
    def _runtime(self, provider):
        return build_repository_runtime(
            True, "owner/repo", "token", lambda: "call-1", provider=provider
        )

    def _broker(self, runtime):
        return CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )

    @staticmethod
    def _authority(permissions: frozenset[str]) -> AuthorityContext:
        return AuthorityContext(
            principal_reference="alx",
            granted_permission_references=permissions,
            evaluated_at=datetime.now(UTC),
        )

    @staticmethod
    def _call(head: str = HEAD, number: int = 21) -> CapabilityCall:
        return CapabilityCall(
            "call-1",
            MERGE_PULL_REQUEST,
            {"pull_request_number": number, "head_sha": head},
        )

    def test_the_granted_permission_alone_permits_a_merge(self) -> None:
        """The delegation itself: no approval is sought, and none is needed."""
        provider = RecordingProvider()
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(), self._authority(frozenset({REPOSITORY_MERGE_PERMISSION}))
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(attempt.result.values["merged"])
        self.assertEqual(len(provider.requests), 1)

    def test_without_the_permission_the_merge_is_never_attempted(self) -> None:
        """A runtime never given the authority cannot merge by accident."""
        provider = RecordingProvider()
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(), self._authority(frozenset({"web.read"}))
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertFalse(attempt.implementation_invoked)
        self.assertEqual(attempt.reason_code, "permission_missing")
        # The decisive assertion: GitHub was never contacted.
        self.assertEqual(provider.requests, [])

    def test_a_missing_policy_fails_closed(self) -> None:
        provider = RecordingProvider()
        runtime = self._runtime(provider)
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions), SafetyGate({}), runtime.executors
        )
        attempt = broker.dispatch(
            self._call(), self._authority(frozenset({REPOSITORY_MERGE_PERMISSION}))
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(attempt.reason_code, "policy_missing")
        self.assertEqual(provider.requests, [])

    def test_a_head_that_moved_after_the_review_is_refused(self) -> None:
        """GitHub answers 409 when the head no longer matches, and that is final."""
        provider = RecordingProvider(error="head_changed")
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(), self._authority(frozenset({REPOSITORY_MERGE_PERMISSION}))
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "head_changed")

    def test_the_exact_reviewed_head_reaches_github(self) -> None:
        """The authorisation is about one revision, so one revision is sent."""
        provider = RecordingProvider()
        self._broker(self._runtime(provider)).dispatch(
            self._call(head=OTHER, number=7),
            self._authority(frozenset({REPOSITORY_MERGE_PERMISSION})),
        )
        self.assertEqual(provider.requests[0].head_sha, OTHER)
        self.assertEqual(provider.requests[0].pull_request_number, 7)

    def test_the_head_must_be_a_full_commit_id(self) -> None:
        """An abbreviation could match a commit nobody reviewed."""
        for head in ("", HEAD[:12], HEAD[:39], "z" * 40, HEAD.upper()):
            with self.subTest(head=head):
                with self.assertRaises(ValueError):
                    MergeRequest(pull_request_number=1, head_sha=head)

    def test_an_unusable_argument_is_a_declared_failure(self) -> None:
        provider = RecordingProvider()
        attempt = self._broker(self._runtime(provider)).dispatch(
            CapabilityCall(
                "call-1",
                MERGE_PULL_REQUEST,
                {"pull_request_number": 1, "head_sha": "short"},
            ),
            self._authority(frozenset({REPOSITORY_MERGE_PERMISSION})),
        )
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "arguments_unusable")
        self.assertEqual(provider.requests, [])

    def test_branch_protection_refusal_is_reported_not_worked_around(self) -> None:
        provider = RecordingProvider(error="merge_refused")
        attempt = self._broker(self._runtime(provider)).dispatch(
            self._call(), self._authority(frozenset({REPOSITORY_MERGE_PERMISSION}))
        )
        self.assertEqual(attempt.result.failure["code"], "merge_refused")
        # One attempt. Nothing retries a refusal.
        self.assertEqual(len(provider.requests), 1)

    def test_an_undeclared_failure_logs_its_type_but_not_its_wording(self) -> None:
        """An operator needs a cause; the log must not carry the request.

        A provider exception's message can hold the token and the commit text
        AL/X composed, so the class is named and the wording is not.
        """
        import logging

        class Exploding:
            def merge(self, request):
                raise RuntimeError("token=SECRET-abc123 body=unheard wording")

        runtime = self._runtime(Exploding())
        with self.assertLogs("alx.tools.repository", level=logging.WARNING) as logs:
            attempt = self._broker(runtime).dispatch(
                self._call(), self._authority(frozenset({REPOSITORY_MERGE_PERMISSION}))
            )
        self.assertEqual(attempt.result.failure["code"], "merge_unavailable")
        recorded = "\n".join(logs.output)
        self.assertIn("RuntimeError", recorded)
        self.assertNotIn("SECRET-abc123", recorded)
        self.assertNotIn("unheard wording", recorded)

    def test_the_policy_requires_no_person_approval(self) -> None:
        """Friedl delegated the authority rather than approving each merge."""
        runtime = self._runtime(RecordingProvider())
        policy = runtime.policies[MERGE_PULL_REQUEST]
        self.assertEqual(
            policy.permission_references, frozenset({REPOSITORY_MERGE_PERMISSION})
        )
        self.assertFalse(policy.approval_required)
        self.assertFalse(policy.standing_scope_allowed)

    def test_the_capability_declares_that_it_publishes_authored_text(self) -> None:
        """The optional title and message become commit metadata.

        They are wording AL/X composed for somewhere other than this
        conversation, so the Core's check on text Friedl has not heard must
        apply. An external review caught this test asserting the opposite.
        """
        runtime = self._runtime(RecordingProvider())
        definition = runtime.definitions[0]
        self.assertIs(definition.side_effect, SideEffect.EFFECTFUL)
        self.assertTrue(definition.transmits_authored_text)

    def test_an_unconfigured_runtime_registers_nothing(self) -> None:
        """Honestly absent rather than registered and always failing."""
        self.assertIsNone(
            build_repository_runtime(False, "owner/repo", "t", lambda: "c")
        )
        self.assertIsNone(build_repository_runtime(True, "", "t", lambda: "c"))
        self.assertIsNone(build_repository_runtime(True, "owner/repo", "", lambda: "c"))


class MergeProviderTest(unittest.TestCase):
    """The GitHub call itself, without contacting GitHub."""

    def _provider(self, status: int, body: object, headers: dict | None = None):
        from alx.providers import github_merge

        supplied = dict(headers or {})

        class Response:
            status_code = status

            def __init__(self) -> None:
                self.headers = supplied

            @staticmethod
            def json():
                if body is None:
                    raise ValueError("no body")
                return body

        sent: dict = {}

        def put(url, json, headers, timeout):  # noqa: A002
            sent["url"] = url
            sent["json"] = json
            return Response()

        original = github_merge.httpx.put
        github_merge.httpx.put = put
        self.addCleanup(setattr, github_merge.httpx, "put", original)
        return github_merge.GitHubMergeProvider("owner/repo", "token"), sent

    def test_the_reviewed_head_is_sent_as_sha(self) -> None:
        """This is what makes GitHub refuse a head that moved."""
        provider, sent = self._provider(200, {"merged": True, "sha": "c" * 40})
        provider.merge(MergeRequest(pull_request_number=21, head_sha=HEAD))
        self.assertEqual(sent["json"]["sha"], HEAD)
        self.assertEqual(sent["json"]["merge_method"], "squash")
        self.assertIn("/pulls/21/merge", sent["url"])

    def test_a_conflicting_head_is_reported_as_head_changed(self) -> None:
        provider, _ = self._provider(409, {"message": "Head branch was modified"})
        with self.assertRaises(MergeError) as caught:
            provider.merge(MergeRequest(pull_request_number=21, head_sha=HEAD))
        self.assertEqual(caught.exception.code, "head_changed")

    def test_protection_and_validation_refusals_are_declared(self) -> None:
        for status in (403, 405, 422):
            with self.subTest(status=status):
                provider, _ = self._provider(status, {"message": "no"})
                with self.assertRaises(MergeError) as caught:
                    provider.merge(MergeRequest(pull_request_number=21, head_sha=HEAD))
                self.assertEqual(caught.exception.code, "merge_refused")

    def test_throttling_is_not_reported_as_a_refusal(self) -> None:
        """A rate limit means try later; a refusal means the merge was rejected.

        GitHub answers 403 for both, so the headers decide. Reporting a limit
        as a refusal would tell AL/X the change was rejected when it was only
        delayed.
        """
        for headers in (
            {"retry-after": "60"},
            {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "5000"},
        ):
            with self.subTest(headers=headers):
                provider, _ = self._provider(403, {"message": "rate limited"}, headers)
                with self.assertRaises(MergeError) as caught:
                    provider.merge(MergeRequest(pull_request_number=21, head_sha=HEAD))
                self.assertEqual(caught.exception.code, "merge_unavailable")

    def test_a_genuine_refusal_is_still_a_refusal(self) -> None:
        provider, _ = self._provider(
            403, {"message": "protected branch"}, {"x-ratelimit-remaining": "4999"}
        )
        with self.assertRaises(MergeError) as caught:
            provider.merge(MergeRequest(pull_request_number=21, head_sha=HEAD))
        self.assertEqual(caught.exception.code, "merge_refused")

    def test_a_malformed_repository_is_refused_at_construction(self) -> None:
        """A wrong endpoint would surface only as a generic failure at merge."""
        from alx.providers.github_merge import GitHubMergeProvider

        for repository in ("/repo", "owner/", "owner/repo/extra", "ownerrepo", ""):
            with self.subTest(repository=repository):
                with self.assertRaises(ValueError):
                    GitHubMergeProvider(repository, "token")
        GitHubMergeProvider("alx-1977/AL-X", "token")

    def test_a_response_that_did_not_merge_is_never_reported_as_merged(self) -> None:
        provider, _ = self._provider(200, {"merged": False})
        with self.assertRaises(MergeError) as caught:
            provider.merge(MergeRequest(pull_request_number=21, head_sha=HEAD))
        self.assertEqual(caught.exception.code, "merge_refused")


if __name__ == "__main__":
    unittest.main()
