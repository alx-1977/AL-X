"""Putting a repair where a reviewer and CI can see it.

The Coding Agent commits inside its worktree and stops there, by construction.
Until these capabilities existed the work stopped there too: law gates run on
pull requests and every reviewer watches them, so a commit that never left the
machine could not be reviewed, checked, or merged.

These exercise the real providers. The git commands are run against actual
repositories in a temporary directory rather than asserted as strings, because
what matters is what git does with them — a refspec that reads as a deletion,
or a branch name that resolves to a tag, is not visible in a string comparison.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts.publication import (  # noqa: E402
    PROTECTED_BRANCHES,
    PublicationError,
    PublicationRequest,
    PullRequestError,
    PullRequestRequest,
    publishable_branch,
)
from alx.providers.github_pull_request import GitHubPullRequests  # noqa: E402
from alx.providers.repository_publication import (
    RepositoryPublication,
    _git_environment,
)  # noqa: E402
from alx.tools.publication import (  # noqa: E402
    DEFINITIONS,
    OPEN_PULL_REQUEST,
    PUBLISH_REPAIR_BRANCH,
    build_publication_executors,
)

HEAD = "a" * 40


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


class BranchNameTests(unittest.TestCase):
    """What may be published is decided before anything runs."""

    def test_the_default_branch_is_never_publishable(self) -> None:
        """A repair reaches main by being reviewed and merged."""
        for branch in PROTECTED_BRANCHES:
            with self.subTest(branch=branch):
                self.assertFalse(publishable_branch(branch))
                with self.assertRaises(ValueError):
                    PublicationRequest(branch=branch, head_sha=HEAD)

    def test_a_repair_branch_is_publishable(self) -> None:
        for branch in ("fix/thing", "feat/x", "repair-1", "a.b_c"):
            with self.subTest(branch=branch):
                self.assertTrue(publishable_branch(branch))

    def test_nothing_that_could_read_as_an_option_or_a_refspec(self) -> None:
        """The argv is built from this name, so its shape is the guard."""
        for branch in (
            "--force", "-f", "../evil", "a b", "refs/heads/x", "x:y",
            # Real newlines, not the two characters that spell one. A name git
            # would otherwise accept, with a newline appended, is what proves
            # the pattern is anchored at the end rather than merely searched.
            "+x", "x\n", "fix/thing\n", "", "   ", "a/b/c",
        ):
            with self.subTest(branch=branch):
                self.assertFalse(publishable_branch(branch))

    def test_the_revision_must_be_a_full_commit_id(self) -> None:
        for sha in ("", "abc", "A" * 40, "g" * 40, HEAD + "\n"):
            with self.subTest(sha=sha):
                with self.assertRaises(ValueError):
                    PublicationRequest(branch="fix/thing", head_sha=sha)


class RealRepositoryHarness(unittest.TestCase):
    """Real repositories, real git, real pushes between them.

    Held apart from the tests so another case can reuse the repositories
    without also re-running everything asserted against them.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.remote = root / "remote.git"
        self.local = root / "local"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.local)],
            capture_output=True, check=True,
        )
        git(self.local, "config", "user.email", "test@example.test")
        git(self.local, "config", "user.name", "Test")
        (self.local / "seed.txt").write_text("seed\n")
        git(self.local, "add", "seed.txt")
        git(self.local, "commit", "-m", "seed")
        git(self.local, "push", "origin", "main")
        self.publication = RepositoryPublication(self.local)

    def branch(self, name: str, content: str = "work\n") -> str:
        git(self.local, "checkout", "-q", "-b", name)
        (self.local / "work.txt").write_text(content)
        git(self.local, "add", "work.txt")
        git(self.local, "commit", "-m", f"work on {name}")
        return git(self.local, "rev-parse", "HEAD")

    def remote_sha(self, name: str) -> str:
        return git(self.remote, "rev-parse", f"refs/heads/{name}")


class PublicationTests(RealRepositoryHarness):
    """What publishing guarantees, against real git."""

    def test_a_repair_branch_reaches_the_remote_at_that_commit(self) -> None:
        sha = self.branch("fix/thing")
        outcome = self.publication.publish(
            PublicationRequest(branch="fix/thing", head_sha=sha)
        )
        self.assertTrue(outcome.published)
        self.assertEqual(outcome.head_sha, sha)
        self.assertEqual(self.remote_sha("fix/thing"), sha)

    def test_publishing_the_same_commit_again_is_not_new_work(self) -> None:
        sha = self.branch("fix/thing")
        self.publication.publish(PublicationRequest("fix/thing", sha))
        again = self.publication.publish(PublicationRequest("fix/thing", sha))
        self.assertTrue(again.published)
        self.assertTrue(again.already_current)

    def test_the_default_branch_is_refused_before_git_runs(self) -> None:
        main_sha = git(self.local, "rev-parse", "refs/heads/main")
        for branch in ("main", "master", "HEAD"):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    PublicationRequest(branch=branch, head_sha=main_sha)

    def test_a_branch_that_does_not_exist_is_refused(self) -> None:
        with self.assertRaises(PublicationError) as caught:
            self.publication.publish(PublicationRequest("fix/absent", HEAD))
        self.assertEqual(caught.exception.code, "branch_unknown")

    def test_a_commit_the_branch_does_not_point_at_is_refused(self) -> None:
        """The revision is asserted, not read.

        Work written after the decision must not travel under it, so a branch
        that moved between deciding and pushing is refused rather than
        published at whatever it now points at.
        """
        self.branch("fix/thing")
        (self.local / "work.txt").write_text("later\n")
        git(self.local, "add", "work.txt")
        git(self.local, "commit", "-m", "later work")
        stale = git(self.local, "rev-parse", "HEAD~1")
        with self.assertRaises(PublicationError) as caught:
            self.publication.publish(PublicationRequest("fix/thing", stale))
        self.assertEqual(caught.exception.code, "branch_unknown")

    def test_a_diverged_remote_is_refused_and_never_overwritten(self) -> None:
        """No force and no lease: what the remote holds, it keeps."""
        sha = self.branch("fix/thing")
        self.publication.publish(PublicationRequest("fix/thing", sha))
        remote_before = self.remote_sha("fix/thing")

        # Someone else advances the remote branch.
        other = Path(self.directory.name) / "other"
        subprocess.run(
            ["git", "clone", "-b", "fix/thing", str(self.remote), str(other)],
            capture_output=True, check=True,
        )
        git(other, "config", "user.email", "other@example.test")
        git(other, "config", "user.name", "Other")
        (other / "theirs.txt").write_text("theirs\n")
        git(other, "add", "theirs.txt")
        git(other, "commit", "-m", "their work")
        git(other, "push", "origin", "fix/thing")
        theirs = self.remote_sha("fix/thing")
        self.assertNotEqual(theirs, remote_before)

        # Ours now diverges. It must be refused, and theirs must survive.
        (self.local / "work.txt").write_text("ours\n")
        git(self.local, "add", "work.txt")
        git(self.local, "commit", "-m", "our work")
        ours = git(self.local, "rev-parse", "HEAD")
        with self.assertRaises(PublicationError) as caught:
            self.publication.publish(PublicationRequest("fix/thing", ours))
        self.assertEqual(caught.exception.code, "branch_diverged")
        self.assertEqual(self.remote_sha("fix/thing"), theirs)

    def test_no_force_or_deletion_shape_can_be_produced(self) -> None:
        """Authority by enumeration: the argv is built, never passed through."""
        commands: list[list[str]] = []

        def runner(argv, **keywords):
            commands.append(argv)
            raise AssertionError("no command should run in this test")

        publication = RepositoryPublication(self.local, runner=runner)
        with self.assertRaises(AssertionError):
            publication.publish(PublicationRequest("fix/thing", HEAD))
        flat = " ".join(" ".join(command) for command in commands)
        for forbidden in (
            "--force", "-f", "--force-with-lease", "--delete", "--mirror",
            "--all", "--tags",
        ):
            self.assertNotIn(forbidden, flat)

    def test_only_origin_is_ever_pushed_to(self) -> None:
        sha = self.branch("fix/thing")
        commands: list[list[str]] = []
        real = subprocess.run

        def runner(argv, **keywords):
            commands.append(argv)
            return real(argv, **keywords)

        RepositoryPublication(self.local, runner=runner).publish(
            PublicationRequest("fix/thing", sha)
        )
        pushes = [command for command in commands if "push" in command]
        self.assertEqual(len(pushes), 1)
        self.assertIn("origin", pushes[0])
        # Both sides named, so the destination cannot come from configuration
        # and cannot be a deletion, which is an empty source.
        self.assertIn("refs/heads/fix/thing:refs/heads/fix/thing", pushes[0])


class FakeGitHub:
    """GitHub's pull request endpoints, answering the production calls."""

    def __init__(self, existing: list | None = None, status: int = 200,
                 headers: dict | None = None) -> None:
        self.existing = existing if existing is not None else []
        self.created: list[dict] = []
        self.queries: list[str] = []
        self.status = status
        self.headers = headers or {}

    def request(self, method: str, url: str, **keywords):
        outer = self

        class Response:
            def __init__(self, payload, status=200) -> None:
                self.status_code = status
                self.headers: dict = outer.headers
                self._payload = payload

            def json(self):
                return self._payload

        if method == "GET" and "/pulls?" in url:
            self.queries.append(url)
            if self.status != 200:
                return Response([], self.status)
            return Response(self.existing)
        if method == "POST" and url.endswith("/pulls"):
            payload = keywords.get("json") or {}
            self.created.append(payload)
            return Response({
                "number": 99,
                "state": "open",
                "head": {"sha": HEAD, "ref": payload.get("head")},
                "base": {"ref": payload.get("base")},
            })
        raise AssertionError(f"unexpected call: {method} {url}")


class PullRequestTests(unittest.TestCase):
    def provider(self, existing=None) -> GitHubPullRequests:
        self.github = FakeGitHub(existing)
        import alx.providers.github_pull_request as module

        original = module.httpx.request
        module.httpx.request = self.github.request
        self.addCleanup(setattr, module.httpx, "request", original)
        return GitHubPullRequests("owner/repo", "token")

    def test_a_published_branch_opens_a_pull_request_into_main(self) -> None:
        outcome = self.provider().open(
            PullRequestRequest(branch="fix/thing", title="Fix the thing")
        )
        self.assertTrue(outcome.created)
        self.assertEqual(outcome.pull_request_number, 99)
        self.assertEqual(outcome.head_sha, HEAD)
        self.assertEqual(outcome.base, "main")

    def test_the_base_cannot_be_chosen(self) -> None:
        """A pull request into somewhere unwatched is a review nobody does."""
        self.provider().open(PullRequestRequest("fix/thing", "Fix"))
        self.assertEqual(self.github.created[0]["base"], "main")
        self.assertNotIn("head_ref", self.github.created[0])

    def test_an_existing_pull_request_is_reused_rather_than_duplicated(self) -> None:
        """Two pull requests for one branch split the review in half."""
        existing = [{
            "number": 42,
            "state": "open",
            "head": {"sha": HEAD, "ref": "fix/thing"},
            "base": {"ref": "main"},
        }]
        outcome = self.provider(existing).open(
            PullRequestRequest("fix/thing", "Fix the thing")
        )
        self.assertFalse(outcome.created)
        self.assertEqual(outcome.pull_request_number, 42)
        self.assertEqual(self.github.created, [])

    def test_the_default_branch_cannot_be_proposed(self) -> None:
        for branch in ("main", "master"):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    PullRequestRequest(branch=branch, title="No")

    def test_a_pull_request_needs_a_title(self) -> None:
        with self.assertRaises(ValueError):
            PullRequestRequest(branch="fix/thing", title="   ")


class CapabilityTests(unittest.TestCase):
    """The executors AL/X actually reaches, over the real providers."""

    def executors(self, publication, pull_requests):
        return build_publication_executors(
            publication, pull_requests, lambda: "call-1"
        )

    def test_unusable_arguments_never_reach_the_provider(self) -> None:
        class Exploding:
            def publish(self, request):
                raise AssertionError("must not be reached")

            def open(self, request):
                raise AssertionError("must not be reached")

        executors = self.executors(Exploding(), Exploding())
        published = executors[PUBLISH_REPAIR_BRANCH]({"branch": "main",
                                                      "head_sha": HEAD})
        self.assertEqual(published.failure["code"], "arguments_unusable")
        opened = executors[OPEN_PULL_REQUEST]({"branch": "main", "title": "x"})
        self.assertEqual(opened.failure["code"], "arguments_unusable")

    def test_a_provider_failure_is_reported_as_its_declared_code(self) -> None:
        class Failing:
            def publish(self, request):
                raise PublicationError("branch_diverged")

            def open(self, request):
                raise PullRequestError("pull_request_refused")

        executors = self.executors(Failing(), Failing())
        published = executors[PUBLISH_REPAIR_BRANCH](
            {"branch": "fix/thing", "head_sha": HEAD}
        )
        self.assertEqual(published.failure["code"], "branch_diverged")
        opened = executors[OPEN_PULL_REQUEST]({"branch": "fix/thing", "title": "x"})
        self.assertEqual(opened.failure["code"], "pull_request_refused")

    def test_neither_capability_can_merge(self) -> None:
        """Publishing proposes work. Merging is a separate authority.

        Asserted against what the module can actually reach, not against its
        text: a scan for the word matched its own prose explaining that it
        does not merge, which is the reconstruction trap rather than a check.
        """
        import ast

        tree = ast.parse(
            (REPOSITORY_ROOT / "src/alx/tools/publication.py").read_text()
        )
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("github_merge", "MergeRequest", "MERGE_PULL_REQUEST"):
            self.assertNotIn(forbidden, imported)
        # The capabilities it declares, in full: two, and neither merges.
        self.assertEqual(
            [item.capability_id for item in DEFINITIONS],
            [PUBLISH_REPAIR_BRANCH, OPEN_PULL_REQUEST],
        )


class ReviewerIdentityTests(unittest.TestCase):
    """Reviewer identity is an authentication boundary.

    Review evidence reaches AL/X's reasoning and can complete a watched task,
    so an account that passes this check can put findings in front of her that
    she will weigh as a reviewer's. Matching the revision exactly does not
    authenticate who wrote about it.

    This began as a prefix, which accepted every lookalike anyone could
    register. These hold the exact-allowlist boundary that replaced it.
    """

    def profile(self, provider):
        from alx.contracts.review_provider import profile_for

        return profile_for(provider)

    def test_the_real_reviewer_accounts_are_accepted(self) -> None:
        from alx.contracts.review_provider import ReviewProvider

        cases = {
            ReviewProvider.CODERABBIT: ["coderabbitai[bot]"],
            # Greptile publishes under both, and neither is guessed at.
            ReviewProvider.GREPTILE: ["greptile-apps[bot]", "greptile[bot]"],
        }
        for provider, logins in cases.items():
            for login in logins:
                with self.subTest(provider=provider.value, login=login):
                    self.assertTrue(self.profile(provider).authored_by_reviewer(login))

    def test_a_lookalike_account_is_refused(self) -> None:
        """Anyone can register a name that merely starts the same way."""
        from alx.contracts.review_provider import ReviewProvider

        cases = {
            ReviewProvider.CODERABBIT: [
                "coderabbit-evil[bot]", "coderabbitXYZ", "coderabbit",
                "coderabbitai", "coderabbitai[bot]x", "xcoderabbitai[bot]",
            ],
            ReviewProvider.GREPTILE: [
                "greptile-evil[bot]", "greptileXYZ", "greptile-apps",
                "greptileai[bot]", "greptile[bot]-x",
            ],
        }
        for provider, logins in cases.items():
            for login in logins:
                with self.subTest(provider=provider.value, login=login):
                    self.assertFalse(self.profile(provider).authored_by_reviewer(login))

    def test_case_is_the_only_normalisation(self) -> None:
        """GitHub logins are case-insensitive, so the same account may differ."""
        from alx.contracts.review_provider import ReviewProvider

        profile = self.profile(ReviewProvider.CODERABBIT)
        self.assertTrue(profile.authored_by_reviewer("CodeRabbitAI[bot]"))
        self.assertTrue(profile.authored_by_reviewer("  coderabbitai[bot]  "))

    def test_nothing_that_is_not_a_login_is_accepted(self) -> None:
        from alx.contracts.review_provider import ReviewProvider

        profile = self.profile(ReviewProvider.CODERABBIT)
        for value in (None, 1, "", "   ", [], {"login": "coderabbitai[bot]"}):
            with self.subTest(value=value):
                self.assertFalse(profile.authored_by_reviewer(value))

    def test_one_provider_never_accepts_another_s_account(self) -> None:
        from alx.contracts.review_provider import ReviewProvider

        self.assertFalse(
            self.profile(ReviewProvider.CODERABBIT)
            .authored_by_reviewer("greptile[bot]")
        )
        self.assertFalse(
            self.profile(ReviewProvider.GREPTILE)
            .authored_by_reviewer("coderabbitai[bot]")
        )

    def test_no_provider_matches_by_prefix_or_substring(self) -> None:
        """Structural: the check is membership, never a shape comparison."""
        import ast

        from alx.contracts.review_provider import ReviewProviderProfile

        source = (
            REPOSITORY_ROOT / "src/alx/contracts/review_provider.py"
        ).read_text()
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "authored_by_reviewer"
            ):
                reached = {
                    item.func.attr
                    for item in ast.walk(node)
                    if isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Attribute)
                }
                for forbidden in ("startswith", "endswith", "match", "search"):
                    self.assertNotIn(forbidden, reached)
                return
        raise AssertionError("the identity check is missing")


class PullRequestIdentityTests(PullRequestTests):
    """A pull request is identified by head *and* base, not head alone.

    Filtering on head alone returned any open pull request from the branch,
    including one into some other base — so `open_pull_request` could hand back
    a proposal that no gate runs on and no reviewer watches, reported as though
    the work had been put up for review.
    """

    def test_the_reuse_lookup_asks_for_the_fixed_base(self) -> None:
        self.provider().open(PullRequestRequest("fix/thing", "Fix"))
        self.assertTrue(self.github.queries)
        self.assertIn("base=main", self.github.queries[0])
        self.assertIn("state=open", self.github.queries[0])

    def test_a_pull_request_into_another_base_is_not_reused(self) -> None:
        """The answer is checked again, not trusted because it was filtered."""
        other_base = [{
            "number": 42,
            "state": "open",
            "head": {"sha": HEAD, "ref": "fix/thing"},
            "base": {"ref": "release/1.0"},
        }]
        outcome = self.provider(other_base).open(
            PullRequestRequest("fix/thing", "Fix the thing")
        )
        # Not reused: a new one is opened into main instead.
        self.assertTrue(outcome.created)
        self.assertEqual(outcome.base, "main")
        self.assertEqual(self.github.created[0]["base"], "main")

    def test_a_pull_request_from_another_branch_is_not_reused(self) -> None:
        wrong_head = [{
            "number": 42,
            "state": "open",
            "head": {"sha": HEAD, "ref": "fix/other"},
            "base": {"ref": "main"},
        }]
        outcome = self.provider(wrong_head).open(
            PullRequestRequest("fix/thing", "Fix")
        )
        self.assertTrue(outcome.created)

    def test_a_closed_pull_request_is_not_reused(self) -> None:
        closed = [{
            "number": 42,
            "state": "closed",
            "head": {"sha": HEAD, "ref": "fix/thing"},
            "base": {"ref": "main"},
        }]
        outcome = self.provider(closed).open(PullRequestRequest("fix/thing", "Fix"))
        self.assertTrue(outcome.created)

    def test_the_returned_evidence_always_states_the_fixed_base(self) -> None:
        outcome = self.provider().open(PullRequestRequest("fix/thing", "Fix"))
        self.assertEqual(outcome.base, "main")


class GitHubThrottleTests(PullRequestTests):
    """"Try later" and "no" are different answers, on both GitHub paths.

    GitHub rate-limits with 403 and a retry header rather than 429. The review
    path knew that and the publication path did not, so the same status meant
    two different things depending on which provider asked — one would have
    reported a throttle as a capability failure.
    """

    def test_a_throttled_403_is_unavailable_not_refused(self) -> None:
        for headers in ({"Retry-After": "60"}, {"X-RateLimit-Remaining": "0"}):
            with self.subTest(headers=headers):
                self.github = FakeGitHub(status=403, headers=headers)
                import alx.providers.github_pull_request as module

                original = module.httpx.request
                module.httpx.request = self.github.request
                self.addCleanup(setattr, module.httpx, "request", original)
                provider = GitHubPullRequests("owner/repo", "token")
                with self.assertRaises(PullRequestError) as caught:
                    provider.open(PullRequestRequest("fix/thing", "Fix"))
                self.assertEqual(caught.exception.code, "pull_request_unavailable")

    def test_a_bare_403_is_still_a_refusal(self) -> None:
        """Without the evidence GitHub sends with a limit, it is a refusal."""
        self.github = FakeGitHub(status=403)
        import alx.providers.github_pull_request as module

        original = module.httpx.request
        module.httpx.request = self.github.request
        self.addCleanup(setattr, module.httpx, "request", original)
        with self.assertRaises(PullRequestError) as caught:
            GitHubPullRequests("owner/repo", "token").open(
                PullRequestRequest("fix/thing", "Fix")
            )
        self.assertEqual(caught.exception.code, "pull_request_refused")

    def test_both_providers_share_one_reading(self) -> None:
        """Structural: neither provider restates the rule for itself."""
        import ast

        for relative in (
            "src/alx/providers/github_pull_request.py",
            "src/alx/providers/github_review.py",
        ):
            with self.subTest(module=relative):
                source = (REPOSITORY_ROOT / relative).read_text()
                literals = {
                    node.value
                    for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                }
                # The header names live in the shared helper alone.
                self.assertNotIn("Retry-After", literals)
                self.assertNotIn("X-RateLimit-Remaining", literals)
                self.assertIn("unavailable", source)


class GitLocaleTests(RealRepositoryHarness):
    """Git's wording is parsed, so the locale that decides it is set here.

    Two facts this module reports are read from git's own English text: that a
    refusal was a divergence rather than a plain rejection, and that the remote
    already had the revision. Git translates both when the environment asks it
    to. Inheriting the user's locale therefore made the push safe but its
    description wrong — a diverged remote reported as `publication_refused`, an
    already-published branch reported as newly published.

    These run the real publication path with a translated locale set in the
    parent environment. Whether this machine has git's translations installed
    decides whether the parent locale could have changed the wording, so the
    binding is asserted on the environment the production code builds, and the
    outcomes are asserted end to end through real pushes.
    """

    TRANSLATED = {"LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8",
                  "LC_CTYPE": "de_DE.UTF-8", "LC_MESSAGES": "de_DE.UTF-8"}

    def setUp(self) -> None:
        super().setUp()
        for name, value in self.TRANSLATED.items():
            patched = unittest.mock.patch.dict(os.environ, {name: value})
            patched.start()
            self.addCleanup(patched.stop)

    def test_the_git_environment_pins_the_locale(self) -> None:
        """Set, not inherited: the parent's locale cannot reach git."""
        environment = _git_environment()
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual(environment["LANG"], "C")
        # Nothing else carries a locale through either.
        for name in ("LC_CTYPE", "LC_MESSAGES", "LANGUAGE"):
            self.assertNotIn(name, environment)

    def test_a_branch_still_publishes_under_a_translated_parent(self) -> None:
        sha = self.branch("fix/thing")
        outcome = self.publication.publish(
            PublicationRequest(branch="fix/thing", head_sha=sha)
        )
        self.assertTrue(outcome.published)
        self.assertEqual(self.remote_sha("fix/thing"), sha)

    def test_up_to_date_is_still_recognised_under_a_translated_parent(self) -> None:
        """The already-current reading is git's words, so it is at risk."""
        sha = self.branch("fix/thing")
        self.publication.publish(PublicationRequest("fix/thing", sha))
        again = self.publication.publish(PublicationRequest("fix/thing", sha))
        self.assertTrue(again.already_current)
        # Published either way: the branch is on the remote at that commit.
        self.assertTrue(again.published)

    def test_divergence_is_still_recognised_under_a_translated_parent(self) -> None:
        """The other reading taken from git's words, end to end."""
        sha = self.branch("fix/thing")
        self.publication.publish(PublicationRequest("fix/thing", sha))
        other = Path(self.directory.name) / "other"
        subprocess.run(
            ["git", "clone", "-b", "fix/thing", str(self.remote), str(other)],
            capture_output=True, check=True,
        )
        git(other, "config", "user.email", "other@example.test")
        git(other, "config", "user.name", "Other")
        (other / "theirs.txt").write_text("theirs\n")
        git(other, "add", "theirs.txt")
        git(other, "commit", "-m", "their work")
        git(other, "push", "origin", "fix/thing")
        theirs = self.remote_sha("fix/thing")

        (self.local / "work.txt").write_text("ours\n")
        git(self.local, "add", "work.txt")
        git(self.local, "commit", "-m", "our work")
        ours = git(self.local, "rev-parse", "HEAD")
        with self.assertRaises(PublicationError) as caught:
            self.publication.publish(PublicationRequest("fix/thing", ours))
        self.assertEqual(caught.exception.code, "branch_diverged")
        self.assertEqual(self.remote_sha("fix/thing"), theirs)


if __name__ == "__main__":
    unittest.main()
