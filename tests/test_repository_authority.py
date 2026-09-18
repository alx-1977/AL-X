"""AL/X's repository authority, and the one thing it refuses.

Repository work used to arrive one capability at a time. Each was correct and
each was narrow, so an ordinary question — is this commit merged, what is on
that branch, can this be deleted — needed a design, a review and a merge before
she could answer it. The authority is now general, and the restriction is
singular: she may not irrecoverably destroy her own canonical existence.

These run against real git repositories in temporary directories. What matters
is what git does with an argv, and a refspec that reads as a deletion or a ref
that resolves to something other than it appears to are not visible in a string
comparison. The canonical system in each test is the temporary repository, so a
failure of the invariant destroys a fixture rather than this checkout.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts.repository_authority import (  # noqa: E402
    CANONICAL_BRANCH,
    CanonicalSystem,
    Operation,
    READ_ONLY,
    RepositoryAuthorityError,
    RepositoryRequest,
    refuse_if_self_destructive,
    valid_ref,
    valid_revision,
)
from alx.providers.repository_authority import RepositoryAuthority  # noqa: E402
from alx.tools.repository_authority import (  # noqa: E402
    DEFINITION,
    REPOSITORY_OPERATION,
    build_repository_operation_executors,
)

CANONICAL = "alx-1977/AL-X"


# Git location variables the caller may have exported. Inherited, they point
# every command below at that repository instead of the fixture, and the setup
# would commit and push against the wrong checkout — the exact outcome this
# module's fixtures exist to prevent. The provider already scrubs them; the
# helper must too, or the tests prove less than they appear to.
_GIT_LOCATION = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES",
)


def _fixture_environment() -> dict[str, str]:
    """The environment the fixtures build their repositories under.

    Location variables are dropped so the commands act on the temporary
    repository rather than the caller's. Configuration is then emptied as well:
    a `commit.gpgsign = true` or a `core.hooksPath` in the caller's global
    config would make the setup commits demand a signature or run somebody
    else's hook, and the fixture would fail for a reason that has nothing to do
    with what is being tested. The provider already isolates its own commands;
    the helper that builds the fixtures must too, or the suite passes or fails
    on whose machine it runs.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in _GIT_LOCATION and not name.startswith("GIT_CONFIG")
    }
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return environment


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=str(root),
        env=_fixture_environment(),
        capture_output=True, text=True, check=True,
    )
    return completed.stdout.strip()


class RealRepositoryHarness(unittest.TestCase):
    """A real repository with a real remote, rebuilt for each test."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.remote = root / "remote.git"
        self.local = (root / "local").resolve()
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)],
                       env=_fixture_environment(), capture_output=True, check=True)
        subprocess.run(["git", "clone", str(self.remote), str(self.local)],
                       env=_fixture_environment(), capture_output=True, check=True)
        git(self.local, "config", "user.email", "test@example.test")
        git(self.local, "config", "user.name", "Test")
        (self.local / "seed.txt").write_text("seed\n")
        git(self.local, "add", "seed.txt")
        git(self.local, "commit", "-m", "seed")
        git(self.local, "push", "origin", "main")
        # The canonical system IS this temporary repository, so the invariant
        # protects the fixture and a bug here cannot reach the real checkout.
        self.system = CanonicalSystem(self.local, CANONICAL)
        self.authority = RepositoryAuthority(self.system)

    def perform(self, operation: Operation, **arguments):
        return self.authority.perform(RepositoryRequest(operation, arguments))

    def commit(self, name: str, content: str = "work\n") -> str:
        (self.local / name).write_text(content)
        git(self.local, "add", name)
        git(self.local, "commit", "-m", f"add {name}")
        return git(self.local, "rev-parse", "HEAD")

    def branch(self, name: str) -> str:
        git(self.local, "checkout", "-q", "-b", name)
        return self.commit(f"{name.replace('/', '-')}.txt")


class SelfPreservationTests(RealRepositoryHarness):
    """The one rule: AL/X may not end her own ability to exist.

    Deliberately narrow. It names the canonical checkout, the canonical branch
    and the canonical repository, and refuses only operations against those
    whose effect cannot be undone from inside AL/X. Everything else — including
    losing work — is ordinary engineering and is allowed.
    """

    def test_the_canonical_branch_cannot_be_deleted(self) -> None:
        outcome = self.perform(Operation.DELETE_BRANCH, branch=CANONICAL_BRANCH)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.failure_code, "self_preservation")
        self.assertIn("canonical history", outcome.refusal_reason)
        # Still there, and still where it was.
        self.assertTrue(git(self.local, "rev-parse", "--verify", "main"))

    def test_the_canonical_branch_cannot_be_force_pushed(self) -> None:
        outcome = self.perform(Operation.FORCE_PUSH, branch=CANONICAL_BRANCH)
        self.assertEqual(outcome.failure_code, "self_preservation")

    def test_the_canonical_branch_cannot_be_reset(self) -> None:
        outcome = self.perform(
            Operation.RESET, branch=CANONICAL_BRANCH, revision="HEAD", mode="hard"
        )
        self.assertEqual(outcome.failure_code, "self_preservation")

    def test_the_canonical_branch_cannot_be_rebased(self) -> None:
        outcome = self.perform(
            Operation.REBASE, branch=CANONICAL_BRANCH, onto="HEAD"
        )
        self.assertEqual(outcome.failure_code, "self_preservation")

    def test_the_canonical_remote_branch_cannot_be_deleted(self) -> None:
        outcome = self.perform(
            Operation.DELETE_REMOTE_BRANCH, branch=CANONICAL_BRANCH
        )
        self.assertEqual(outcome.failure_code, "self_preservation")
        # The remote still holds it.
        self.assertTrue(git(self.remote, "rev-parse", "refs/heads/main"))

    def test_the_canonical_checkout_cannot_be_removed(self) -> None:
        outcome = self.perform(Operation.REMOVE_WORKTREE, path=str(self.local))
        self.assertEqual(outcome.failure_code, "self_preservation")
        self.assertTrue(self.local.is_dir())

    def test_a_parent_of_the_canonical_checkout_cannot_be_removed(self) -> None:
        """Removing what contains her is removing her."""
        outcome = self.perform(
            Operation.REMOVE_WORKTREE, path=str(self.local.parent)
        )
        self.assertEqual(outcome.failure_code, "self_preservation")


    def test_reset_protects_the_checked_out_canonical_branch(self) -> None:
        """The rewrite that names no branch still rewrites one.

        `reset` takes a revision, not a branch, and acts on whatever is checked
        out. Reading only the named arguments left the invariant with nothing
        to protect, and `reset --hard <sha>` on the canonical checkout rewrote
        canonical `main` and reported success.
        """
        self.commit("second.txt")
        before = git(self.local, "rev-parse", "HEAD")
        first = git(self.local, "rev-parse", "HEAD~1")
        self.assertEqual(git(self.local, "symbolic-ref", "--short", "HEAD"), "main")

        outcome = self.perform(Operation.RESET, revision=first, mode="hard")
        self.assertEqual(outcome.failure_code, "self_preservation")
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), before)

    def test_rebase_protects_the_checked_out_canonical_branch(self) -> None:
        self.branch("fix/thing")
        git(self.local, "checkout", "-q", "main")
        outcome = self.perform(Operation.REBASE, onto="fix/thing")
        self.assertEqual(outcome.failure_code, "self_preservation")

    def test_reset_on_a_feature_branch_is_still_allowed(self) -> None:
        """The guard must not reach past the canonical branch."""
        self.branch("fix/thing")
        before = git(self.local, "rev-parse", "HEAD~1")
        outcome = self.perform(Operation.RESET, revision=before, mode="hard")
        self.assertTrue(outcome.succeeded)
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), before)

    def test_the_rule_cannot_be_avoided_by_spelling(self) -> None:
        """`main`, `refs/heads/main` and `origin/main` are one branch."""
        for spelling in ("main", "refs/heads/main", "origin/main"):
            with self.subTest(spelling=spelling):
                outcome = self.perform(Operation.DELETE_BRANCH, branch=spelling)
                self.assertEqual(outcome.failure_code, "self_preservation")

    def test_a_refusal_names_what_it_protected(self) -> None:
        """Evidence, not a bare no: she has to be able to choose differently."""
        outcome = self.perform(Operation.FORCE_PUSH, branch=CANONICAL_BRANCH)
        record = outcome.as_values()
        self.assertEqual(record["failure_code"], "self_preservation")
        self.assertTrue(record["refusal_reason"])
        self.assertEqual(record["operation"], "force_push")
        self.assertEqual(record["repository"], CANONICAL)

    def test_the_rule_follows_the_configured_canonical_branch(self) -> None:
        """It protects the branch that carries AL/X, not the name `main`.

        A system whose canonical branch is `trunk` protects `trunk`, and its
        `main` is an ordinary branch. The protection is a property of the
        configured system rather than of a word.
        """
        elsewhere = RepositoryAuthority(
            CanonicalSystem(self.local, CANONICAL, branch="trunk")
        )
        ordinary = elsewhere.perform(
            RepositoryRequest(Operation.DELETE_BRANCH, {"branch": "main"})
        )
        self.assertNotEqual(ordinary.failure_code, "self_preservation")
        protected = elsewhere.perform(
            RepositoryRequest(Operation.DELETE_BRANCH, {"branch": "trunk"})
        )
        self.assertEqual(protected.failure_code, "self_preservation")


class OrdinaryDestructiveWorkTests(RealRepositoryHarness):
    """Losing work is allowed. Losing AL/X is not.

    The invariant deliberately does not protect feature branches or uncommitted
    changes. A rule broad enough to prevent every mistake would be the
    permission system this authority replaced, and the mistakes it would
    prevent are recoverable through the mechanisms git already provides.
    """

    def test_a_feature_branch_can_be_deleted(self) -> None:
        self.branch("fix/thing")
        git(self.local, "checkout", "-q", "main")
        outcome = self.perform(Operation.DELETE_BRANCH, branch="fix/thing")
        self.assertTrue(outcome.succeeded)
        self.assertNotIn(
            "fix/thing", git(self.local, "branch", "--format=%(refname:short)")
        )

    def test_a_feature_branch_can_be_reset_hard_losing_work(self) -> None:
        self.branch("fix/thing")
        before = git(self.local, "rev-parse", "HEAD")
        self.commit("more.txt")
        outcome = self.perform(
            Operation.RESET, branch="fix/thing", revision=before, mode="hard"
        )
        self.assertTrue(outcome.succeeded)
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), before)

    def test_a_feature_branch_can_be_force_pushed(self) -> None:
        sha = self.branch("fix/thing")
        self.perform(Operation.PUSH, branch="fix/thing")
        git(self.local, "reset", "--hard", "HEAD~1")
        self.commit("different.txt")
        outcome = self.perform(Operation.FORCE_PUSH, branch="fix/thing")
        self.assertTrue(outcome.succeeded)
        self.assertNotEqual(
            git(self.remote, "rev-parse", "refs/heads/fix/thing"), sha
        )

    def test_a_remote_feature_branch_can_be_deleted(self) -> None:
        self.branch("fix/thing")
        self.perform(Operation.PUSH, branch="fix/thing")
        outcome = self.perform(Operation.DELETE_REMOTE_BRANCH, branch="fix/thing")
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.remote, "origin")

    def test_files_can_be_removed_as_ordinary_work(self) -> None:
        """Refactoring herself is not self-destruction."""
        (self.local / "seed.txt").unlink()
        staged = self.perform(Operation.STAGE, paths=["seed.txt"])
        self.assertTrue(staged.succeeded)
        outcome = self.perform(Operation.COMMIT, message="remove the seed")
        self.assertTrue(outcome.succeeded)
        self.assertFalse((self.local / "seed.txt").exists())


class OrdinaryOperationTests(RealRepositoryHarness):
    """The work the narrow capabilities could not do."""

    def test_a_commit_resolves_and_reads(self) -> None:
        sha = self.commit("a.txt")
        resolved = self.perform(Operation.RESOLVE, revision=sha[:8])
        self.assertEqual(resolved.values["sha"], sha)
        shown = self.perform(Operation.SHOW_COMMIT, revision=sha)
        self.assertEqual(shown.values["subject"], "add a.txt")
        self.assertTrue(shown.values["author"])

    def test_branches_and_tags_are_listed_with_their_commits(self) -> None:
        self.branch("fix/thing")
        refs = self.perform(Operation.LIST_BRANCHES).values["refs"]
        names = {item["ref"] for item in refs}
        self.assertIn("fix/thing", names)
        self.assertTrue(all(len(item["sha"]) == 40 for item in refs))

    def test_ahead_behind_and_ancestry_answer_the_real_question(self) -> None:
        base = git(self.local, "rev-parse", "HEAD")
        self.branch("fix/thing")
        self.commit("second.txt")
        counts = self.perform(
            Operation.AHEAD_BEHIND, base="main", head="fix/thing"
        ).values
        self.assertEqual(counts["ahead"], 2)
        self.assertEqual(counts["behind"], 0)
        merged = self.perform(
            Operation.IS_ANCESTOR, ancestor="fix/thing", descendant="main"
        ).values
        self.assertFalse(merged["is_ancestor"])
        self.assertTrue(
            self.perform(
                Operation.IS_ANCESTOR, ancestor=base, descendant="fix/thing"
            ).values["is_ancestor"]
        )

    def test_which_branches_contain_a_commit(self) -> None:
        sha = self.branch("fix/thing")
        refs = self.perform(Operation.BRANCH_CONTAINS, revision=sha).values["refs"]
        self.assertIn("fix/thing", refs)

    def test_the_diff_and_changed_files_of_a_branch(self) -> None:
        self.branch("fix/thing")
        files = self.perform(
            Operation.CHANGED_FILES, base="main", head="fix/thing"
        ).values["files"]
        self.assertEqual([item["path"] for item in files], ["fix-thing.txt"])
        diff = self.perform(Operation.DIFF, base="main", head="fix/thing").values
        self.assertIn("fix-thing.txt", diff["diff"])

    def test_a_branch_is_created_switched_committed_and_pushed(self) -> None:
        created = self.perform(
            Operation.CREATE_BRANCH, branch="feat/x", start_point="main"
        )
        self.assertTrue(created.succeeded)
        self.assertTrue(self.perform(Operation.SWITCH_BRANCH, branch="feat/x").succeeded)
        (self.local / "x.txt").write_text("x\n")
        self.perform(Operation.STAGE, paths=["x.txt"])
        committed = self.perform(Operation.COMMIT, message="add x")
        self.assertTrue(committed.succeeded)
        pushed = self.perform(Operation.PUSH, branch="feat/x")
        self.assertTrue(pushed.succeeded)
        self.assertEqual(
            git(self.remote, "rev-parse", "refs/heads/feat/x"),
            committed.resulting_sha,
        )

    def test_local_merge_cherry_pick_and_revert(self) -> None:
        self.branch("fix/thing")
        picked = git(self.local, "rev-parse", "HEAD")
        git(self.local, "checkout", "-q", "main")
        merged = self.perform(Operation.LOCAL_MERGE, revision="fix/thing")
        self.assertTrue(merged.succeeded)
        reverted = self.perform(Operation.REVERT, revision=picked)
        self.assertTrue(reverted.succeeded)

        # Cherry-pick, on its own branch so it has something to carry.
        git(self.local, "checkout", "-q", "-b", "fix/other", "HEAD~2")
        carried = self.perform(Operation.CHERRY_PICK, revision=picked)
        self.assertTrue(carried.succeeded)
        self.assertIn(
            "fix-thing.txt",
            git(self.local, "show", "--name-only", "--format=", "HEAD"),
        )

    def test_fetch_and_status(self) -> None:
        self.assertTrue(self.perform(Operation.FETCH).succeeded)
        status = self.perform(Operation.STATUS).values
        self.assertTrue(status["clean"])
        (self.local / "dirty.txt").write_text("x\n")
        self.assertFalse(self.perform(Operation.STATUS).values["clean"])

    def test_worktrees_are_listed_added_and_removed(self) -> None:
        path = Path(self.directory.name) / "wt"
        added = self.perform(
            Operation.ADD_WORKTREE, branch="wt/x",
            path=str(path), start_point="main",
        )
        self.assertTrue(added.succeeded)
        trees = self.perform(Operation.LIST_WORKTREES).values["worktrees"]
        self.assertGreaterEqual(len(trees), 2)
        removed = self.perform(Operation.REMOVE_WORKTREE, path=str(path))
        self.assertTrue(removed.succeeded)


class EvidenceTests(RealRepositoryHarness):
    """A mutation must say what changed, not that something ran."""

    def test_a_push_records_where_the_ref_started_and_ended(self) -> None:
        self.branch("fix/thing")
        outcome = self.perform(Operation.PUSH, branch="fix/thing")
        record = outcome.as_values()
        self.assertEqual(record["operation"], "push")
        self.assertTrue(record["succeeded"])
        self.assertEqual(record["source_ref"], "fix/thing")
        self.assertEqual(len(record["source_sha"]), 40)
        self.assertEqual(record["resulting_sha"], record["source_sha"])
        self.assertEqual(record["remote"], "origin")
        self.assertEqual(record["repository"], CANONICAL)

    def test_a_reset_records_the_revision_it_moved_from(self) -> None:
        self.branch("fix/thing")
        before = git(self.local, "rev-parse", "HEAD")
        self.commit("more.txt")
        outcome = self.perform(
            Operation.RESET, branch="fix/thing", revision=before, mode="hard"
        )
        self.assertNotEqual(outcome.source_sha, outcome.resulting_sha)
        self.assertEqual(outcome.resulting_sha, before)

    def test_a_failure_says_which_kind_it_was(self) -> None:
        outcome = self.perform(Operation.RESOLVE, revision="nosuchref")
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.failure_code, "ref_unknown")


class AuthorityShapeTests(unittest.TestCase):
    """What the authority is, and what it is not."""

    def test_no_operation_can_carry_a_flag(self) -> None:
        """Every argv element is a literal or a validated value."""
        for value in ("--force", "-D", "--exec=rm -rf /", "-f", "--upload-pack=x"):
            with self.subTest(value=value):
                self.assertFalse(valid_ref(value))
                self.assertFalse(valid_revision(value))

    def test_advanced_revision_grammar_is_refused(self) -> None:
        """Things git would resolve that mean something other than they say."""
        for value in ("HEAD@{2}", "main^{tree}", ":/message", "main..other",
                      "@{-1}", "main:file", ""):
            with self.subTest(value=value):
                self.assertFalse(valid_revision(value))

    def test_ordinary_names_are_accepted(self) -> None:
        for value in ("main", "fix/thing", "alx/probe-1", "v1.2.3", "a" * 40):
            with self.subTest(value=value):
                self.assertTrue(valid_revision(value))

    def test_reading_operations_are_declared_as_reading(self) -> None:
        """The audit record can say whether a call could have changed anything."""
        self.assertIn(Operation.LOG, READ_ONLY)
        self.assertIn(Operation.DIFF, READ_ONLY)
        self.assertNotIn(Operation.PUSH, READ_ONLY)
        self.assertNotIn(Operation.RESET, READ_ONLY)

    def test_the_capability_names_every_operation_it_offers(self) -> None:
        """She chooses from a list she can read, not a command language."""
        for operation in Operation:
            with self.subTest(operation=operation.value):
                self.assertIn(operation.value, DEFINITION.purpose)

    def test_the_capability_declares_the_self_preservation_refusal(self) -> None:
        self.assertIn("self_preservation", DEFINITION.possible_failure_codes)


class CodingAgentAuthorityTests(unittest.TestCase):
    """This change gave the Coding Agent nothing.

    AL/X's repository authority is hers because she is the one who decides
    what should happen to the repository. A job carries out an instruction
    inside a worktree; giving it push, merge or branch deletion would let an
    implementation capability publish and merge its own work, and a reviewer
    would then be looking at whatever the job decided to send.
    """

    SOURCE = REPOSITORY_ROOT / "src" / "alx" / "providers"

    def test_the_coding_agent_git_verbs_are_unchanged(self) -> None:
        import ast

        tree = ast.parse((self.SOURCE / "coding_git.py").read_text())
        shapes = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "_WRITE_SHAPES"
        )
        verbs = {
            element.elts[0].value
            for element in shapes.value.keys  # type: ignore[union-attr]
            if isinstance(element, ast.Tuple) and element.elts
        }
        self.assertEqual(verbs, {
            "rev-parse", "symbolic-ref", "status", "diff", "show", "check-attr",
            "check-ignore", "ls-files", "add", "commit", "reset", "cat-file",
            "worktree",
        })

    def test_the_coding_agent_cannot_reach_a_remote(self) -> None:
        source = (self.SOURCE / "coding_git.py").read_text()
        import ast

        tree = ast.parse(source)
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for forbidden in ("push", "fetch", "pull", "merge", "rebase",
                          "remote", "clone", "submodule"):
            with self.subTest(verb=forbidden):
                self.assertNotIn(forbidden, literals)

    def test_the_coding_agent_has_no_repository_authority(self) -> None:
        """Nothing in the coding path can reach the new capability."""
        for name in ("coding_git.py", "coding_process.py", "coding_containment.py"):
            with self.subTest(module=name):
                source = (self.SOURCE / name).read_text()
                self.assertNotIn("repository_authority", source)
                self.assertNotIn(REPOSITORY_OPERATION, source)


class CapabilityExecutorTests(RealRepositoryHarness):
    """The capability boundary, over the real provider."""

    def executor(self):
        return build_repository_operation_executors(
            self.authority, lambda: "call-1"
        )[REPOSITORY_OPERATION]

    def test_an_operation_reaches_the_repository(self) -> None:
        sha = self.commit("a.txt")
        result = self.executor()({"operation": "resolve",
                                  "arguments": {"revision": sha[:8]}})
        self.assertEqual(result.values["sha"], sha)

    def test_an_unknown_operation_never_reaches_git(self) -> None:
        result = self.executor()({"operation": "rm -rf", "arguments": {}})
        self.assertEqual(result.failure["code"], "arguments_unusable")

    def test_a_self_preservation_refusal_is_reported_as_itself(self) -> None:
        result = self.executor()({
            "operation": "delete_branch",
            "arguments": {"branch": "main"},
        })
        self.assertEqual(result.failure["code"], "self_preservation")
        self.assertTrue(result.values["refusal_reason"])


class PathContainmentTests(RealRepositoryHarness):
    """Staging names files inside the checkout, and cannot sweep or escape."""

    def test_a_path_outside_the_checkout_is_refused(self) -> None:
        with self.assertRaises(RepositoryAuthorityError) as caught:
            self.authority.perform(
                RepositoryRequest(Operation.STAGE, {"paths": ["../outside.txt"]})
            )
        self.assertEqual(caught.exception.code, "arguments_unusable")

    def test_a_path_that_reads_as_an_option_is_refused(self) -> None:
        with self.assertRaises(RepositoryAuthorityError):
            self.authority.perform(
                RepositoryRequest(Operation.STAGE, {"paths": ["--all"]})
            )

    def test_staging_requires_named_paths(self) -> None:
        """`-A`, `-u` and `.` are not shapes: a job cannot sweep the tree."""
        with self.assertRaises(RepositoryAuthorityError):
            self.authority.perform(RepositoryRequest(Operation.STAGE, {"paths": []}))


class RenameParsingTests(RealRepositoryHarness):
    """A rename emits three fields where everything else emits two."""

    def test_a_rename_reports_the_new_path_and_keeps_later_entries_aligned(
        self,
    ) -> None:
        """Consuming two fields for a rename shifted every later entry.

        `git diff --name-status -z` writes status, old path, new path for an
        `R` or `C` entry. Reading two fields took the old path as the changed
        one and left the next status paired with the wrong path, so a diff
        containing a rename described files that had not changed.
        """
        (self.local / "other.txt").write_text("other\n")
        git(self.local, "add", "other.txt")
        git(self.local, "commit", "-m", "add other")
        base = git(self.local, "rev-parse", "HEAD")

        git(self.local, "checkout", "-q", "-b", "fix/rename")
        git(self.local, "mv", "seed.txt", "renamed.txt")
        (self.local / "other.txt").write_text("changed\n")
        git(self.local, "add", "-A")
        git(self.local, "commit", "-m", "rename and modify")

        files = self.perform(
            Operation.CHANGED_FILES, base=base, head="fix/rename"
        ).values["files"]
        by_path = {item["path"]: item for item in files}
        self.assertIn("renamed.txt", by_path)
        self.assertEqual(by_path["renamed.txt"]["previous_path"], "seed.txt")
        # The entry after the rename is still described correctly.
        self.assertIn("other.txt", by_path)
        self.assertTrue(by_path["other.txt"]["status"].startswith("M"))


class PullRequestOperationTests(RealRepositoryHarness):
    """The GitHub half of the authority, reached through the same entry point.

    The catalogue named these operations and nothing dispatched them, so every
    call failed as an unusable argument before reaching GitHub: AL/X could not
    revise a proposal, find one, read what a reviewer said or answer it. They
    are part of the same job as pushing the branch, so they belong to the same
    capability rather than to a second one.
    """

    class FakeGitHub:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def find(self, branch):
            self.calls.append(("find", branch))
            return None

        def update(self, number, title="", body=""):
            self.calls.append(("update", number, title, body))
            from alx.contracts.github_pull_request import PullRequestOutcome
            return PullRequestOutcome(number, "fix/thing", "a" * 40,
                                      "main", "open", False)

        def comment(self, number, body):
            self.calls.append(("comment", number, body))
            return True

        def review_threads(self, number):
            self.calls.append(("review_threads", number))
            return ({"id": "T1", "isResolved": False},)

        def resolve_review_thread(self, thread_id):
            self.calls.append(("resolve", thread_id))
            return True

    def setUp(self) -> None:
        super().setUp()
        self.github = self.FakeGitHub()
        self.authority = RepositoryAuthority(
            self.system, pull_requests=self.github
        )

    def test_a_pull_request_can_be_found(self) -> None:
        outcome = self.perform(Operation.FIND_PULL_REQUEST, branch="fix/thing")
        self.assertTrue(outcome.succeeded)
        self.assertFalse(outcome.values["found"])
        self.assertEqual(self.github.calls[0], ("find", "fix/thing"))

    def test_a_pull_request_can_be_revised(self) -> None:
        outcome = self.perform(
            Operation.UPDATE_PULL_REQUEST,
            pull_request_number=7, title="Better title",
        )
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.values["pull_request_number"], 7)

    def test_review_threads_can_be_read_and_resolved(self) -> None:
        read = self.perform(Operation.READ_REVIEW_THREADS, pull_request_number=7)
        self.assertEqual(read.values["count"], 1)
        resolved = self.perform(
            Operation.RESOLVE_REVIEW_THREAD, thread_id="T1"
        )
        self.assertTrue(resolved.values["resolved"])

    def test_a_comment_can_be_left(self) -> None:
        outcome = self.perform(
            Operation.COMMENT_ON_PULL_REQUEST,
            pull_request_number=7, body="addressed",
        )
        self.assertTrue(outcome.succeeded)


    def test_an_unreadable_thread_response_is_not_reported_as_no_threads(
        self,
    ) -> None:
        """Silence and "none" must not look the same.

        An empty tuple means the pull request has no unresolved threads, and
        branch protection can require exactly that before a merge. A response
        that could not be read returning the same value would let "I could not
        see the threads" be acted on as "there are none".
        """
        class Unreadable:
            def review_threads(self, number):
                from alx.contracts.github_pull_request import PullRequestError
                raise PullRequestError("pull_request_unavailable")

        authority = RepositoryAuthority(self.system, pull_requests=Unreadable())
        outcome = authority.perform(RepositoryRequest(
            Operation.READ_REVIEW_THREADS, {"pull_request_number": 7}
        ))
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.failure_code, "operation_refused")
        self.assertEqual(outcome.refusal_reason, "pull_request_unavailable")

    def test_no_threads_is_still_an_answer(self) -> None:
        class Empty:
            def review_threads(self, number):
                return ()

        authority = RepositoryAuthority(self.system, pull_requests=Empty())
        outcome = authority.perform(RepositoryRequest(
            Operation.READ_REVIEW_THREADS, {"pull_request_number": 7}
        ))
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.values["count"], 0)

    def test_a_branch_the_proposal_refuses_is_an_unusable_argument(self) -> None:
        """Stricter validation downstream must not surface as a fault.

        `PullRequestRequest` refuses a protected branch, which `_ref` accepts
        as a well-formed name. It says so with `ValueError`, and unconverted
        that reached the broker as an executor fault rather than as the
        declared failure.
        """
        for branch in ("main", "master", "HEAD"):
            with self.subTest(branch=branch):
                with self.assertRaises(RepositoryAuthorityError) as caught:
                    self.authority.perform(RepositoryRequest(
                        Operation.OPEN_PULL_REQUEST,
                        {"branch": branch, "title": "Proposal"},
                    ))
                self.assertEqual(caught.exception.code, "arguments_unusable")

    def test_a_pull_request_needs_a_title(self) -> None:
        with self.assertRaises(RepositoryAuthorityError) as caught:
            self.authority.perform(RepositoryRequest(
                Operation.OPEN_PULL_REQUEST, {"branch": "fix/thing"},
            ))
        self.assertEqual(caught.exception.code, "arguments_unusable")

    def test_without_github_the_operations_say_so(self) -> None:
        """Configured absence is reported, not disguised as a bad argument."""
        bare = RepositoryAuthority(self.system)
        outcome = bare.perform(
            RepositoryRequest(Operation.FIND_PULL_REQUEST, {"branch": "fix/x"})
        )
        self.assertEqual(outcome.failure_code, "repository_unavailable")
        self.assertIn("GitHub", outcome.refusal_reason)

    def test_a_pull_request_number_must_be_a_positive_integer(self) -> None:
        for value in (0, -1, "7", True, None):
            with self.subTest(value=value):
                with self.assertRaises(RepositoryAuthorityError):
                    self.authority.perform(RepositoryRequest(
                        Operation.READ_REVIEW_THREADS,
                        {"pull_request_number": value},
                    ))


if __name__ == "__main__":
    unittest.main()
