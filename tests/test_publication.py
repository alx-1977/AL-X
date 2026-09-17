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

import subprocess
import sys
import tempfile
import unittest
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
from alx.providers.repository_publication import RepositoryPublication  # noqa: E402
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
            "+x", "x\\n", "", "   ", "a/b/c",
        ):
            with self.subTest(branch=branch):
                self.assertFalse(publishable_branch(branch))

    def test_the_revision_must_be_a_full_commit_id(self) -> None:
        for sha in ("", "abc", "A" * 40, "g" * 40, HEAD + "\\n"):
            with self.subTest(sha=sha):
                with self.assertRaises(ValueError):
                    PublicationRequest(branch="fix/thing", head_sha=sha)


class PublicationTests(unittest.TestCase):
    """Real repositories, real git, real pushes between them."""

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

    def __init__(self, existing: list | None = None) -> None:
        self.existing = existing if existing is not None else []
        self.created: list[dict] = []

    def request(self, method: str, url: str, **keywords):
        class Response:
            def __init__(self, payload, status=200) -> None:
                self.status_code = status
                self.headers: dict = {}
                self._payload = payload

            def json(self):
                return self._payload

        if method == "GET" and "/pulls?" in url:
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


if __name__ == "__main__":
    unittest.main()
