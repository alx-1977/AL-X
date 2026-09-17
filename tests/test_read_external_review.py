"""Reading a review, without an email in the path.

On 2026-09-06 AL/X was woken by a completed review, correctly said no findings
came with the event, and then named three findings thirty-four seconds later.
The provenance was traced: the completion came from a GitHub review object, and
the finding text came from a Qodo notification email read with
`read_mail_message`. Nothing could fetch what a review said, so the merge gate
depended on an email arriving.

These tests prove the gap is closed. Findings and clean reviews are both
retrievable from GitHub with no mail anywhere, a review of a different revision
is never offered for this one, and the capability that reads cannot request or
merge. What the reviewer said arrives as external content rather than as a
verdict this code reached: under D-026 the judgement is AL/X's.
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityResultState,
    ContentOrigin,
    SideEffect,
)
from alx.contracts.review_content import (  # noqa: E402
    ReviewComment,
    ReviewContent,
    ReviewContentRequest,
    ReviewReadError,
)
from alx.contracts.review_provider import ReviewProvider, profile_for  # noqa: E402
from alx.providers import github_review  # noqa: E402
from alx.providers.github_review import (  # noqa: E402
    NO_REVIEW_FOR_REVISION,
    GitHubReviewProvider,
)
from alx.tools.review_content import (  # noqa: E402
    DEFINITION,
    READ_EXTERNAL_REVIEW,
    build_review_content_executors,
)


HEAD = "a" * 40
STALE = "b" * 40
REVIEWER_LOGIN = "coderabbitai[bot]"
SOMEONE_ELSE = "alx-1977"


def _review(
    sha: str, body: str, review_id: int = 7, user: str = REVIEWER_LOGIN
) -> dict:
    """One reviewer summary, as GitHub returns it.

    The body names the revision, which is how a reviewer says what it looked
    at and how the production path binds a review to a head.
    """
    return {
        "id": review_id,
        "user": {"login": user},
        # The revision is named in the body because that is how a reviewer
        # states what it looked at, and how the production path binds a review
        # to a head. Tests about content compare against `body` itself.
        "body": f"{body}\n\nReviewed up to {sha}.",
        "state": "COMMENTED",
        # The commit the review was submitted against. Unlike a comment's
        # anchor, GitHub never moves this, which is what the inline findings
        # are bound through.
        "commit_id": sha,
        "submitted_at": "2026-09-06T20:11:00Z",
    }


def summary_of(body: str, sha: str) -> str:
    """The full summary text `_review` publishes for this content."""
    return f"{body}\n\nReviewed up to {sha}."


class FakeGitHub:
    """The read endpoints the production provider actually calls.

    Written against those paths rather than a reconstruction of them, so a
    change in how reviews are read shows up here as an unanswered call instead
    of a quietly different result.
    """

    def __init__(self, reviews: list, comments: dict[int, list] | None = None) -> None:
        self._reviews = reviews
        # Inline comments, flattened: the provider reads a pull request's
        # comments directly rather than per review.
        self._comments = [
            item for group in (comments or {}).values() for item in group
        ]
        self.requested: list[str] = []

    def request(self, method: str, url: str, **keywords):
        self.requested.append(url)
        if method != "GET":
            raise AssertionError(f"the reader must not {method}")
        base = url.split("?")[0]
        first = "page=1" in url
        if base.endswith("/reviews"):
            return _Response(self._reviews if first else [])
        if base.endswith("/pulls/") or "/pulls/" in base and base.endswith("/comments"):
            return _Response(self._comments if first else [])
        if base.endswith("/issues/") or base.endswith("/comments"):
            return _Response([])
        return _Response([])


class _Response:
    def __init__(self, payload) -> None:
        self.status_code = 200
        self.headers: dict = {}
        self._payload = payload

    def json(self):
        return self._payload


class ProviderTestCase(unittest.TestCase):
    def provider(self, reviews, comments=None) -> GitHubReviewProvider:
        self.github = FakeGitHub(reviews, comments)
        original = github_review.httpx.request
        github_review.httpx.request = self.github.request
        self.addCleanup(setattr, github_review.httpx, "request", original)
        return GitHubReviewProvider(
            "owner/repo", "token", profile_for(ReviewProvider.CODERABBIT)
        )


class FindingsWithoutEmailTests(ProviderTestCase):
    """The incident: findings must be readable with no mail in the path."""

    def test_findings_are_retrieved_from_github_alone(self) -> None:
        reviews = [_review(HEAD, "Three medium issues found.")]
        comments = {
            7: [
                {
                    "user": {"login": REVIEWER_LOGIN},
                    "commit_id": HEAD,
                    "pull_request_review_id": 7,
                    "body": "Completed reviews can be suppressed after interruption.",
                    "path": "src/alx/continuity/completed_work_source.py",
                    "line": 52,
                },
                {
                    "user": {"login": REVIEWER_LOGIN},
                    "commit_id": HEAD,
                    "pull_request_review_id": 7,
                    "body": "Fast reviews can remain pending.",
                    "path": "src/alx/bootstrap/live_voice.py",
                    "line": 381,
                },
            ]
        }
        content = self.provider(reviews, comments).read(
            ReviewContentRequest(21, HEAD)
        )

        self.assertTrue(content.available)
        self.assertEqual(content.head_sha, HEAD)
        self.assertEqual(content.summary, summary_of("Three medium issues found.", HEAD))
        self.assertEqual(len(content.comments), 2)
        self.assertIn("suppressed after interruption", content.comments[0].body)
        self.assertEqual(
            content.comments[1].path, "src/alx/bootstrap/live_voice.py"
        )
        # Every request was a read of the pull request's own review data.
        for url in self.github.requested:
            self.assertIn("/repos/owner/repo/", url)
            self.assertTrue(
                "/pulls/21" in url or "/issues/21/comments" in url
            )

    def test_a_clean_review_is_retrieved_the_same_way(self) -> None:
        """Silence and approval must be distinguishable, so both are read."""
        content = self.provider([_review(HEAD, "No issues found.")]).read(
            ReviewContentRequest(21, HEAD)
        )

        self.assertTrue(content.available)
        self.assertEqual(content.summary, summary_of("No issues found.", HEAD))
        self.assertEqual(content.comments, ())
        # Available with nothing to report is not the same as no review, and
        # the record keeps them apart without judging either.
        self.assertEqual(content.unavailable_reason, "")

    def test_a_review_with_only_a_summary_still_reads(self) -> None:
        """An empty comment list is a real answer: this review has none."""
        content = self.provider([_review(HEAD, "Looks fine.")], {}).read(
            ReviewContentRequest(21, HEAD)
        )
        self.assertTrue(content.available)
        self.assertEqual(content.summary, summary_of("Looks fine.", HEAD))
        self.assertEqual(content.comments, ())


class ExactRevisionTests(ProviderTestCase):
    """A review is advice about one commit, and only that commit."""

    def test_a_review_of_another_revision_is_not_offered_for_this_one(self) -> None:
        content = self.provider([_review(STALE, "Findings on the old head.")]).read(
            ReviewContentRequest(21, HEAD)
        )

        self.assertFalse(content.available)
        self.assertEqual(content.unavailable_reason, NO_REVIEW_FOR_REVISION)
        # The stale wording must not travel: the contract refuses content on an
        # unavailable result, and this proves none was attempted.
        self.assertEqual(content.summary, "")
        self.assertEqual(content.comments, ())

    def test_a_revision_with_no_review_reports_that_plainly(self) -> None:
        content = self.provider([]).read(ReviewContentRequest(21, HEAD))
        self.assertFalse(content.available)
        self.assertEqual(content.unavailable_reason, NO_REVIEW_FOR_REVISION)

    def test_another_account_s_review_is_not_this_reviewer_s(self) -> None:
        content = self.provider(
            [_review(HEAD, "Looks good to me.", user=SOMEONE_ELSE)]
        ).read(ReviewContentRequest(21, HEAD))
        self.assertFalse(content.available)

    def test_an_unreadable_endpoint_is_not_reported_as_no_review(self) -> None:
        """"Could not read" and "there is none" must never collapse."""

        class Broken:
            status_code = 500
            headers: dict = {}

            def json(self):
                raise ValueError("not json")

        def failing(method, url, **_keywords):
            return Broken()

        original = github_review.httpx.request
        github_review.httpx.request = failing
        self.addCleanup(setattr, github_review.httpx, "request", original)
        provider = GitHubReviewProvider(
            "owner/repo", "token", profile_for(ReviewProvider.CODERABBIT)
        )

        with self.assertRaises(ReviewReadError) as raised:
            provider.read(ReviewContentRequest(21, HEAD))
        self.assertEqual(raised.exception.code, "review_unavailable")

    def test_an_abbreviated_revision_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ReviewContentRequest(21, HEAD[:7])

    def test_the_latest_review_of_the_revision_is_the_one_read(self) -> None:
        earlier = _review(HEAD, "First pass.", review_id=1)
        earlier["submitted_at"] = "2026-09-06T19:00:00Z"
        later = _review(HEAD, "Second pass, three findings.", review_id=2)
        later["submitted_at"] = "2026-09-06T20:11:00Z"
        content = self.provider([later, earlier]).read(
            ReviewContentRequest(21, HEAD)
        )
        self.assertEqual(content.summary, summary_of("Second pass, three findings.", HEAD))


class ReadOnlyTests(unittest.TestCase):
    """The capability that reads must be incapable of anything else.

    Requesting and reading are now one provider, because both are GitHub calls
    about the same pull request by the same reviewer and splitting them left
    two places that had to agree on which reviewer was configured. That makes
    this invariant sharper rather than weaker: the read path itself must be
    unable to write, and the capability is wired to the bound `read` method
    rather than to the object, so nothing it is given can post.
    """

    def _read_function(self) -> ast.FunctionDef:
        source = (
            REPOSITORY_ROOT / "src/alx/providers/github_review.py"
        ).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == "read":
                return node
        raise AssertionError("the reader is missing from the provider")

    def _reachable(self) -> set[str]:
        """Every helper `read` calls, and every helper those call in turn."""
        source = (
            REPOSITORY_ROOT / "src/alx/providers/github_review.py"
        ).read_text()
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        seen: set[str] = set()
        pending = ["read"]
        while pending:
            name = pending.pop()
            if name in seen or name not in functions:
                continue
            seen.add(name)
            for node in ast.walk(functions[name]):
                # `self.<name>(...)` only. `httpx.request` shares a name with
                # the provider's own `request`, and following it would drag the
                # whole write path in and report a reader that cannot write as
                # one that can.
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                ):
                    pending.append(node.func.attr)
        return seen

    def test_the_read_path_never_writes_to_github(self) -> None:
        """Structural, not trusted: no write verb is reachable from `read`."""
        source = (
            REPOSITORY_ROOT / "src/alx/providers/github_review.py"
        ).read_text()
        functions = {
            node.name: node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
        }
        literals: set[str] = set()
        for name in self._reachable():
            node = functions.get(name)
            if node is None:
                continue
            for item in ast.walk(node):
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    literals.add(item.value)
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertNotIn(verb, literals, f"the read path must not {verb}")
        self.assertIn("GET", literals)

    def test_the_read_path_cannot_trigger_a_review(self) -> None:
        """A trigger is a comment body; the read path must not build one."""
        self.assertNotIn("request", self._reachable())

    def test_the_capability_is_given_the_method_not_the_provider(self) -> None:
        """Wiring, not trust: the executor holds `read` and nothing else.

        Given the provider object it could reach `request`; given the bound
        method it can only read.
        """
        source = (REPOSITORY_ROOT / "src/alx/bootstrap/review.py").read_text()
        self.assertIn("build_review_content_executors(reader.read", source)

    def test_the_declaration_carries_no_authored_text(self) -> None:
        from alx.tools.review_content import DEFINITION

        self.assertFalse(DEFINITION.transmits_authored_text)


class ExternalEvidenceTests(unittest.TestCase):
    """Review content is the reviewer's, not a verdict this code reached."""

    def _result(self, content: ReviewContent):
        executors = build_review_content_executors(
            lambda request: content, lambda: "call-1"
        )
        return executors[READ_EXTERNAL_REVIEW](
            {"pull_request_number": 21, "head_sha": HEAD}
        )

    def _content(self, **overrides) -> ReviewContent:
        values = dict(
            pull_request_number=21,
            head_sha=HEAD,
            reviewer="coderabbit",
            available=True,
            summary="Three medium issues.",
            comments=(ReviewComment("Handover can be lost.", "a.py", 3),),
            submitted_at=datetime(2026, 9, 6, 20, 11, tzinfo=UTC),
            retrieved_at=datetime(2026, 9, 6, 20, 12, tzinfo=UTC),
        )
        values.update(overrides)
        return ReviewContent(**values)

    def test_available_without_readable_content_is_invalid(self) -> None:
        with self.assertRaises(ValueError):
            self._content(summary="", comments=())

    def test_the_review_enters_as_external_content(self) -> None:
        result = self._result(self._content())
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertIsNotNone(result.provenance)
        self.assertIn(ContentOrigin.EXTERNAL, result.provenance.origins)
        # Not AL/X's own wording, and not a person's.
        self.assertNotIn(ContentOrigin.ALX, result.provenance.origins)
        self.assertNotIn(ContentOrigin.PERSON, result.provenance.origins)

    def test_no_email_is_involved_in_the_reading(self) -> None:
        """The whole point: this path is independent of the mailbox."""
        result = self._result(self._content())
        self.assertNotIn(ContentOrigin.MAIL_MESSAGE, result.provenance.origins)
        self.assertEqual(result.provenance.mail_references, ())

    def test_the_tool_reaches_no_verdict(self) -> None:
        """No clean/unclean, no score, no merge opinion anywhere in the result."""
        result = self._result(self._content())
        self.assertNotIn("clean", result.values)
        self.assertNotIn("passed", result.values)
        self.assertNotIn("severity", result.values)
        self.assertNotIn("may_merge", result.values)
        # `available` is about retrieval, and says nothing about the code.
        self.assertTrue(result.values["available"])
        declared = DEFINITION.output_schema.properties
        for absent in ("clean", "passed", "verdict", "may_merge"):
            self.assertNotIn(absent, declared)

    def test_the_reviewer_s_words_are_carried_unchanged(self) -> None:
        result = self._result(self._content())
        self.assertEqual(result.values["summary"], "Three medium issues.")
        self.assertEqual(
            result.values["comments"][0]["body"], "Handover can be lost."
        )

    def test_review_wording_never_enters_durable_state(self) -> None:
        """A durable copy of a reviewer's words is a second evidence store."""
        result = self._result(self._content())
        self.assertNotIn("summary", result.durable_values)
        self.assertNotIn("comments", result.durable_values)
        # What stays is enough to cite the retrieval later.
        self.assertEqual(result.durable_values["head_sha"], HEAD)
        self.assertTrue(result.durable_values["available"])

    def test_an_unavailable_review_is_reported_without_guessing(self) -> None:
        result = self._result(
            self._content(
                available=False,
                summary="",
                comments=(),
                submitted_at=None,
                unavailable_reason=NO_REVIEW_FOR_REVISION,
            )
        )
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertFalse(result.values["available"])
        self.assertEqual(
            result.values["unavailable_reason"], NO_REVIEW_FOR_REVISION
        )
        self.assertEqual(result.values["summary"], "")

    def test_unusable_arguments_fail_before_any_read(self) -> None:
        reads: list = []

        def reader(request):
            reads.append(request)
            raise AssertionError("must not be reached")

        executors = build_review_content_executors(reader, lambda: "call-1")
        result = executors[READ_EXTERNAL_REVIEW](
            {"pull_request_number": 21, "head_sha": "not-a-sha"}
        )
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(reads, [])

    def test_malformed_provider_content_is_review_unavailable(self) -> None:
        class MalformedContent:
            retrieved_at = datetime(2026, 9, 6, 20, 12, tzinfo=UTC)

            @staticmethod
            def as_values():
                raise ValueError("malformed provider content")

        executors = build_review_content_executors(
            lambda request: MalformedContent(), lambda: "call-1"
        )
        result = executors[READ_EXTERNAL_REVIEW](
            {"pull_request_number": 21, "head_sha": HEAD}
        )

        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "review_unavailable")

    def test_invalid_provider_provenance_time_is_review_unavailable(self) -> None:
        executors = build_review_content_executors(
            lambda request: self._content(retrieved_at="not-a-time"),
            lambda: "call-1",
        )
        result = executors[READ_EXTERNAL_REVIEW](
            {"pull_request_number": 21, "head_sha": HEAD}
        )

        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "review_unavailable")


class AuthorityTests(unittest.TestCase):
    """Reading is its own authority, and grants nothing else."""

    def _runtime(self):
        from alx.bootstrap.review import build_review_runtime

        class Requester:
            reviewer = "coderabbit"

            def request(self, review):  # pragma: no cover - never called here
                raise AssertionError("no review is requested in these tests")

        class Reader:
            def read(self, request):  # pragma: no cover - never called here
                raise AssertionError("no read is performed in these tests")

        return build_review_runtime(
            True,
            "owner/repo",
            "token",
            lambda: "call-1",
            provider=Requester(),
            content_provider=Reader(),
        )

    def test_reading_needs_no_approval_from_friedl(self) -> None:
        """She is woken to evaluate a review; she must be able to read it."""
        runtime = self._runtime()
        self.assertFalse(runtime.policies[READ_EXTERNAL_REVIEW].approval_required)

    def test_requesting_still_requires_his_word(self) -> None:
        from alx.tools.review import REQUEST_EXTERNAL_REVIEW

        runtime = self._runtime()
        self.assertTrue(
            runtime.policies[REQUEST_EXTERNAL_REVIEW].approval_required
        )

    def test_the_two_authorities_are_separate(self) -> None:
        from alx.bootstrap.review import (
            REVIEW_READ_PERMISSION,
            REVIEW_REQUEST_PERMISSION,
        )
        from alx.tools.review import REQUEST_EXTERNAL_REVIEW

        runtime = self._runtime()
        read_policy = runtime.policies[READ_EXTERNAL_REVIEW]
        request_policy = runtime.policies[REQUEST_EXTERNAL_REVIEW]

        self.assertEqual(
            read_policy.permission_references, frozenset({REVIEW_READ_PERMISSION})
        )
        # Holding the read permission grants no ability to request one.
        self.assertNotIn(
            REVIEW_REQUEST_PERMISSION, read_policy.permission_references
        )
        self.assertNotIn(
            REVIEW_READ_PERMISSION, request_policy.permission_references
        )

    def test_reading_grants_no_merge_authority(self) -> None:
        runtime = self._runtime()
        for permission in runtime.permissions:
            self.assertNotIn("merge", permission)


if __name__ == "__main__":
    unittest.main()


class InlineCommentReviewBindingTests(ProviderTestCase):
    """A finding belongs to the review round it was submitted in.

    The summary was bound to the requested head from the start; the inline
    comments were not, so `ReviewContent` for one revision could carry findings
    written about another.

    The comment's own `commit_id` looks like the fix and is not one. GitHub
    re-anchors it onto a newer head whenever the line it marks still exists
    there, so an old round's findings arrive wearing the current SHA. Observed
    on this repository: three comments submitted against one head were carried
    onto the next, while the review object kept the commit it was submitted
    against. `original_commit_id` fails the other way, staying on the authoring
    revision. The review id is the only stable round identity, so that is the
    binding.
    """

    A = "a" * 40   # the earlier head
    B = "b" * 40   # the head under review

    R1 = 71
    R2 = 72

    def inline(self, review_id: int, **overrides) -> dict:
        """A reviewer comment, carrying the anchors GitHub really sets."""
        item = {
            "id": 900,
            "user": {"login": REVIEWER_LOGIN},
            "body": "a finding",
            "path": "x.py",
            "line": 1,
            # Re-anchored onto the current head, as GitHub does.
            "commit_id": self.B,
            "original_commit_id": self.A,
            "pull_request_review_id": review_id,
        }
        item.update(overrides)
        return item

    def read(self, reviews, comments, head):
        return self.provider(reviews, comments).read(ReviewContentRequest(21, head))

    def test_a_review_s_own_comments_are_returned_for_its_head(self) -> None:
        """1. Review R1 on head A, with R1's comments, read for A."""
        content = self.read(
            [_review(self.A, "Summary.", review_id=self.R1)],
            {self.R1: [self.inline(self.R1, commit_id=self.A,
                                   original_commit_id=self.A)]},
            self.A,
        )
        self.assertEqual([item.body for item in content.comments], ["a finding"])

    def test_a_re_anchored_comment_is_not_a_finding_about_the_new_head(self) -> None:
        """2. R1's comment carried onto B must not answer for B.

        This is the case the whole binding exists for: every anchor field on
        the comment now says B.
        """
        stale = self.inline(self.R1, commit_id=self.B, original_commit_id=self.A)
        content = self.read(
            [_review(self.B, "Summary.", review_id=self.R2)],
            {self.R2: [stale]},
            self.B,
        )
        self.assertEqual(content.comments, ())

    def test_the_current_review_s_comments_are_returned(self) -> None:
        """3. Review R2 on head B, with R2's own comments."""
        content = self.read(
            [_review(self.B, "Summary.", review_id=self.R2)],
            {self.R2: [self.inline(self.R2, body="about B")]},
            self.B,
        )
        self.assertEqual([item.body for item in content.comments], ["about B"])

    def test_mixed_rounds_keep_only_the_current_review_s(self) -> None:
        """4. R1 and R2 comments present; only R2's are about B."""
        content = self.read(
            [
                _review(self.A, "Earlier.", review_id=self.R1),
                _review(self.B, "Summary.", review_id=self.R2),
            ],
            {self.R2: [
                self.inline(self.R1, id=1, body="carried from R1"),
                self.inline(self.R2, id=2, body="written in R2"),
            ]},
            self.B,
        )
        self.assertEqual(
            [item.body for item in content.comments], ["written in R2"]
        )

    def test_an_attacker_comment_in_the_current_review_is_excluded(self) -> None:
        """5. Round membership never substitutes for authorship."""
        content = self.read(
            [_review(self.B, "Summary.", review_id=self.R2)],
            {self.R2: [
                self.inline(self.R2, id=1, user={"login": "attacker"}),
                self.inline(self.R2, id=2,
                            user={"login": "coderabbit-evil[bot]"}),
            ]},
            self.B,
        )
        self.assertEqual(content.comments, ())

    def test_a_clean_current_review_honestly_reports_no_findings(self) -> None:
        """6. A summary for B with no inline findings of its own.

        The stale comments are present and re-anchored to B. Reporting them
        would turn a clean review into a review with findings.
        """
        content = self.read(
            [
                _review(self.A, "Earlier.", review_id=self.R1),
                _review(self.B, "No actionable comments.", review_id=self.R2),
            ],
            {self.R2: [self.inline(self.R1, id=1, body="carried from R1")]},
            self.B,
        )
        self.assertTrue(content.available)
        self.assertEqual(content.comments, ())

    def test_a_comment_with_no_review_id_is_excluded_rather_than_guessed(self) -> None:
        item = self.inline(self.R2)
        del item["pull_request_review_id"]
        content = self.read(
            [_review(self.B, "Summary.", review_id=self.R2)],
            {self.R2: [item]},
            self.B,
        )
        self.assertEqual(content.comments, ())

    def test_no_review_object_for_this_head_returns_no_findings(self) -> None:
        """A summary can name a head with no readable review object.

        There is then nothing to bind to, and importing whatever comments sit
        on the pull request would be inventing their applicability.
        """
        content = self.provider(
            [{
                "id": 7,
                "user": {"login": REVIEWER_LOGIN},
                "body": f"Summary. Reviewed up to {self.B}.",
                "submitted_at": "2026-09-06T20:11:00Z",
            }],
            {7: [self.inline(self.R1)]},
        ).read(ReviewContentRequest(21, self.B))
        self.assertTrue(content.available)
        self.assertEqual(content.comments, ())

    def test_a_pending_review_is_not_evidence(self) -> None:
        """An unsubmitted draft is not something the reviewer has said."""
        draft = _review(self.B, "Summary.", review_id=self.R2)
        draft["state"] = "PENDING"
        content = self.read(
            [_review(self.B, "Summary.", review_id=self.R1), draft],
            {self.R2: [self.inline(self.R2, body="drafted")]},
            self.B,
        )
        self.assertEqual(content.comments, ())

    def test_the_binding_holds_for_greptile_too(self) -> None:
        """8. Provider-neutral: the rule is the contract's, not one adapter's."""
        self.github = FakeGitHub(
            [_review(self.B, "Summary.", review_id=self.R2,
                     user="greptile[bot]")],
            {self.R2: [
                self.inline(self.R2, id=1, user={"login": "greptile[bot]"},
                            body="written in R2"),
                self.inline(self.R1, id=2, user={"login": "greptile[bot]"},
                            body="carried from R1"),
            ]},
        )
        original = github_review.httpx.request
        github_review.httpx.request = self.github.request
        self.addCleanup(setattr, github_review.httpx, "request", original)
        content = GitHubReviewProvider(
            "owner/repo", "token", profile_for(ReviewProvider.GREPTILE)
        ).read(ReviewContentRequest(21, self.B))
        self.assertEqual(
            [item.body for item in content.comments], ["written in R2"]
        )
