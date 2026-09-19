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


    def test_a_decoy_selector_cannot_redirect_the_invariant(self) -> None:
        """What is protected must be what is acted on.

        `arguments` accepts arbitrary keys and `_argv` ignores `branch` for
        `reset` and `rebase`, so a request carrying a valid revision and an
        unrelated branch had the invariant examine the decoy while the command
        rewrote canonical `main`. Falling back to HEAD only when no selector
        was given was not enough: for these two operations the selector is
        meaningless and is discarded entirely.
        """
        self.commit("second.txt")
        git(self.local, "branch", "fix/decoy")
        before = git(self.local, "rev-parse", "HEAD")
        first = git(self.local, "rev-parse", "HEAD~1")

        for selector in ("branch", "ref", "target"):
            with self.subTest(selector=selector):
                outcome = self.perform(
                    Operation.RESET,
                    revision=first, mode="hard", **{selector: "fix/decoy"},
                )
                self.assertEqual(outcome.failure_code, "self_preservation")
                self.assertEqual(git(self.local, "rev-parse", "HEAD"), before)

    def test_a_decoy_selector_cannot_redirect_a_rebase(self) -> None:
        self.branch("fix/thing")
        git(self.local, "checkout", "-q", "main")
        outcome = self.perform(
            Operation.REBASE, onto="fix/thing", branch="fix/thing"
        )
        self.assertEqual(outcome.failure_code, "self_preservation")

    def test_the_rule_cannot_be_avoided_by_spelling(self) -> None:
        """`main`, `refs/heads/main` and `origin/main` are one branch."""
        for spelling in ("main", "refs/heads/main", "origin/main"):
            with self.subTest(spelling=spelling):
                outcome = self.perform(Operation.DELETE_BRANCH, branch=spelling)
                self.assertEqual(outcome.failure_code, "self_preservation")


    def test_a_branch_merely_ending_in_the_canonical_name_is_ordinary(self) -> None:
        """`fix/main` is a feature branch, not the canonical history.

        Reducing a ref to its last path segment matched every branch whose
        name happens to end that way, so `fix/main`, `feat/main` and the rest
        were refused as though they carried AL/X's history. Fail-closed, so
        nothing was lost — but the invariant is meant to be narrow, and one
        that cannot tell these apart is not.
        """
        for name in ("fix/main", "feat/main", "alx/main", "release/main"):
            with self.subTest(branch=name):
                git(self.local, "branch", name)
                outcome = self.perform(Operation.DELETE_BRANCH, branch=name)
                self.assertTrue(outcome.succeeded)
                self.assertNotIn(
                    name,
                    git(self.local, "branch", "--format=%(refname:short)"),
                )



    def test_a_successful_reset_records_the_branch_it_moved(self) -> None:
        """The success record had the same decoy mismatch as the refusal.

        Three places worked out which ref an operation concerned — the
        invariant, the refusal record and the success record — and the third
        still read the request's arguments. A reset carrying a decoy `branch`
        therefore reported moving a branch that had not moved, with the sha of
        one it never touched.
        """
        self.branch("fix/work")
        git(self.local, "branch", "fix/decoy")
        before = git(self.local, "rev-parse", "HEAD")
        first = git(self.local, "rev-parse", "HEAD~1")

        outcome = self.perform(
            Operation.RESET, revision=first, mode="hard", branch="fix/decoy"
        )
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.source_ref, "fix/work")
        self.assertEqual(outcome.resulting_ref, "fix/work")
        self.assertEqual(outcome.source_sha, before)
        self.assertEqual(outcome.resulting_sha, first)

    def test_a_refusal_names_the_ref_the_command_would_touch(self) -> None:
        """The record must describe the operation the decision was about.

        `reset` and `rebase` act on whatever is checked out, so the invariant
        ignores a named branch — but the audit record read it, and a refusal
        could name a decoy the command would never have touched. Two places
        answering "which ref" separately is how they diverged.
        """
        self.commit("second.txt")
        git(self.local, "branch", "fix/decoy")
        first = git(self.local, "rev-parse", "HEAD~1")
        self.assertEqual(git(self.local, "symbolic-ref", "--short", "HEAD"), "main")

        outcome = self.perform(
            Operation.RESET, revision=first, mode="hard", branch="fix/decoy"
        )
        self.assertEqual(outcome.failure_code, "self_preservation")
        # The branch it would have rewritten, not the one the request named.
        self.assertEqual(outcome.source_ref, "main")

    def test_other_operations_still_record_the_ref_they_name(self) -> None:
        """The change must not reach past reset and rebase."""
        outcome = self.perform(Operation.DELETE_BRANCH, branch="main")
        self.assertEqual(outcome.failure_code, "self_preservation")
        self.assertEqual(outcome.source_ref, "main")

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
        # Off `main` first: git refuses to delete a checked-out branch, so
        # leaving it checked out made this pass on `operation_refused` without
        # the invariant ever being consulted. A regression that protected every
        # branch called `main` would have passed too.
        git(self.local, "branch", "trunk")
        git(self.local, "checkout", "-q", "trunk")
        ordinary = elsewhere.perform(
            RepositoryRequest(Operation.DELETE_BRANCH, {"branch": "main"})
        )
        self.assertTrue(ordinary.succeeded)
        self.assertNotIn(
            "main", git(self.local, "branch", "--format=%(refname:short)")
        )
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


class UnverifiedRemoteTests(RealRepositoryHarness):
    """A checkout whose origin is not the configured repository.

    Withholding the whole authority would leave AL/X unable to read a
    repository's history merely because its remote is unusual, and reading is
    never the risk. What is withheld is everything that reaches the remote:
    work must not travel to a repository nobody has confirmed is this one.
    """

    def setUp(self) -> None:
        super().setUp()
        self.authority = RepositoryAuthority(
            self.system, remote_verified=False
        )

    def test_local_work_is_still_available(self) -> None:
        sha = self.commit("a.txt")
        self.assertEqual(
            self.perform(Operation.RESOLVE, revision=sha[:8]).values["sha"], sha
        )
        self.assertTrue(self.perform(Operation.STATUS).succeeded)
        self.assertTrue(
            self.perform(
                Operation.CREATE_BRANCH, branch="fix/x", start_point="main"
            ).succeeded
        )

    def test_nothing_may_reach_the_remote(self) -> None:
        self.branch("fix/thing")
        for operation, arguments in (
            (Operation.PUSH, {"branch": "fix/thing"}),
            (Operation.FORCE_PUSH, {"branch": "fix/thing"}),
            (Operation.FETCH, {}),
            (Operation.PULL_FAST_FORWARD, {"branch": "main"}),
            (Operation.DELETE_REMOTE_BRANCH, {"branch": "fix/thing"}),
        ):
            with self.subTest(operation=operation.value):
                outcome = self.authority.perform(
                    RepositoryRequest(operation, arguments)
                )
                self.assertFalse(outcome.succeeded)
                self.assertEqual(outcome.failure_code, "operation_refused")
                self.assertIn("origin", outcome.refusal_reason)

    def test_the_remote_is_untouched(self) -> None:
        self.branch("fix/thing")
        self.authority.perform(
            RepositoryRequest(Operation.PUSH, {"branch": "fix/thing"})
        )
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.remote, "rev-parse", "refs/heads/fix/thing")

    def test_a_verified_remote_still_publishes(self) -> None:
        """The gate must not reach a checkout that was confirmed."""
        verified = RepositoryAuthority(self.system, remote_verified=True)
        self.branch("fix/thing")
        outcome = verified.perform(
            RepositoryRequest(Operation.PUSH, {"branch": "fix/thing"})
        )
        self.assertTrue(outcome.succeeded)


class AnswerQualityTests(RealRepositoryHarness):
    """An answer must be an answer, not a guess dressed as one."""

    def test_an_unreadable_ancestry_question_is_a_failure(self) -> None:
        """`--is-ancestor` exits 1 for "no" and something else for "broken".

        Treating every non-zero code as "no" turned "I could not tell" into a
        confident negative on the question AL/X uses to decide whether work is
        already merged — she would read an unknown ref as unmerged work.
        """
        outcome = self.perform(
            Operation.IS_ANCESTOR, ancestor="nosuchref", descendant="main"
        )
        self.assertFalse(outcome.succeeded)
        self.assertNotIn("is_ancestor", outcome.values)

    def test_a_real_ancestry_question_still_answers_both_ways(self) -> None:
        base = git(self.local, "rev-parse", "HEAD")
        self.branch("fix/thing")
        yes = self.perform(
            Operation.IS_ANCESTOR, ancestor=base, descendant="fix/thing"
        )
        self.assertTrue(yes.succeeded)
        self.assertTrue(yes.values["is_ancestor"])
        no = self.perform(
            Operation.IS_ANCESTOR, ancestor="fix/thing", descendant="main"
        )
        self.assertTrue(no.succeeded)
        self.assertFalse(no.values["is_ancestor"])

    def test_a_fast_forward_acts_on_the_branch_it_names(self) -> None:
        """`git merge` advances what is checked out, not what was named.

        Naming `main` while `fix/x` was checked out advanced `fix/x` and
        recorded `main` as the affected ref — a record describing an operation
        that did not happen.
        """
        self.branch("fix/thing")
        before = git(self.local, "rev-parse", "HEAD")
        with self.assertRaises(RepositoryAuthorityError) as caught:
            self.authority.perform(
                RepositoryRequest(Operation.PULL_FAST_FORWARD, {"branch": "main"})
            )
        self.assertEqual(caught.exception.code, "arguments_unusable")
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), before)


class AuditRecordTests(RealRepositoryHarness):
    """A refusal is evidence too, and must say what it refused."""

    def test_a_self_preservation_refusal_names_the_ref_and_commit(self) -> None:
        outcome = self.perform(Operation.DELETE_BRANCH, branch="main")
        record = outcome.as_values()
        self.assertEqual(record["failure_code"], "self_preservation")
        self.assertEqual(record["source_ref"], "main")
        self.assertEqual(len(record["source_sha"]), 40)
        self.assertTrue(record["refusal_reason"])

    def test_a_failed_operation_names_what_it_was_acting_on(self) -> None:
        self.branch("fix/thing")
        outcome = self.perform(Operation.PUSH, branch="fix/thing")
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.remote, self.authority._remote())

    def test_an_unverified_remote_refusal_carries_the_ref(self) -> None:
        blocked = RepositoryAuthority(self.system, remote_verified=False)
        self.branch("fix/thing")
        outcome = blocked.perform(
            RepositoryRequest(Operation.PUSH, {"branch": "fix/thing"})
        )
        record = outcome.as_values()
        self.assertEqual(record["source_ref"], "fix/thing")
        self.assertEqual(len(record["source_sha"]), 40)
        self.assertEqual(record["remote"], "origin")

    def test_a_push_names_the_verified_url_not_the_remote_name(self) -> None:
        """A name is resolved when the command runs, which is after the check.

        Anything able to write `.git/config` between composition and the push
        could point `origin` elsewhere, so the destination that was verified is
        the one named.
        """
        verified = RepositoryAuthority(
            self.system, verified_remote=str(self.remote)
        )
        commands: list[list[str]] = []
        real = subprocess.run

        def runner(argv, **keywords):
            commands.append(list(argv))
            return real(argv, **keywords)

        verified = RepositoryAuthority(
            self.system, runner=runner, verified_remote=str(self.remote)
        )
        self.branch("fix/thing")
        verified.perform(RepositoryRequest(Operation.PUSH, {"branch": "fix/thing"}))
        push = next(command for command in commands if "push" in command)
        self.assertIn(str(self.remote), push)
        self.assertNotIn("origin", push)


class RemoteRefspecTests(RealRepositoryHarness):
    """A URL carries no configured refspec, so what is wanted must be named.

    Binding remote operations to the verified URL closed a redirection gap and
    opened this one: `origin` brings its configured fetch refspec and its
    tracking ref with it, and a URL brings neither. The operations that relied
    on those have to say what they mean explicitly.
    """

    def advance_remote(self) -> str:
        """Another clone pushes, so the remote is ahead of this checkout."""
        other = Path(self.directory.name) / "other"
        subprocess.run(
            ["git", "clone", str(self.remote), str(other)],
            env=_fixture_environment(), capture_output=True, check=True,
        )
        git(other, "config", "user.email", "other@example.test")
        git(other, "config", "user.name", "Other")
        (other / "theirs.txt").write_text("theirs\n")
        git(other, "add", "theirs.txt")
        git(other, "commit", "-m", "their work")
        git(other, "push", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def url_bound(self):
        return RepositoryAuthority(
            self.system, verified_remote=str(self.remote)
        )

    def test_a_fetch_updates_the_tracking_refs(self) -> None:
        """Otherwise it reports success and leaves the checkout stale.

        Git writes FETCH_HEAD either way, so the fetch succeeds while
        `refs/remotes/origin/*` stays where it was — and the fast-forward that
        follows merges old state believing it is current.
        """
        theirs = self.advance_remote()
        before = git(self.local, "rev-parse", "refs/remotes/origin/main")
        self.assertNotEqual(before, theirs)

        outcome = self.url_bound().perform(RepositoryRequest(Operation.FETCH, {}))
        self.assertTrue(outcome.succeeded)
        self.assertEqual(
            git(self.local, "rev-parse", "refs/remotes/origin/main"), theirs
        )

    def test_a_fast_forward_after_fetching_lands_the_remote_work(self) -> None:
        theirs = self.advance_remote()
        authority = self.url_bound()
        authority.perform(RepositoryRequest(Operation.FETCH, {}))
        outcome = authority.perform(
            RepositoryRequest(Operation.PULL_FAST_FORWARD, {"branch": "main"})
        )
        self.assertTrue(outcome.succeeded)
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), theirs)

    def test_a_force_push_names_the_revision_it_expects(self) -> None:
        """The bare lease derives from a tracking ref a URL does not select.

        Without the expected revision the lease is empty, so the protection is
        silently absent and the force is unguarded.
        """
        sha = self.branch("fix/thing")
        authority = self.url_bound()
        authority.perform(RepositoryRequest(Operation.PUSH, {"branch": "fix/thing"}))
        authority.perform(RepositoryRequest(Operation.FETCH, {}))

        commands: list[list[str]] = []
        real = subprocess.run

        def runner(argv, **keywords):
            commands.append(list(argv))
            return real(argv, **keywords)

        watched = RepositoryAuthority(
            self.system, runner=runner, verified_remote=str(self.remote)
        )
        git(self.local, "reset", "--hard", "HEAD~1")
        self.commit("different.txt")
        outcome = watched.perform(
            RepositoryRequest(Operation.FORCE_PUSH, {"branch": "fix/thing"})
        )
        self.assertTrue(outcome.succeeded)
        push = next(command for command in commands if "push" in command)
        lease = next(item for item in push if item.startswith("--force-with-lease"))
        self.assertEqual(lease, f"--force-with-lease=fix/thing:{sha}")

    def test_forcing_without_a_tracking_ref_is_refused(self) -> None:
        """Nothing to lease against is not a reason to force unguarded."""
        self.branch("fix/unpushed")
        with self.assertRaises(RepositoryAuthorityError) as caught:
            self.url_bound().perform(
                RepositoryRequest(Operation.FORCE_PUSH, {"branch": "fix/unpushed"})
            )
        self.assertEqual(caught.exception.code, "arguments_unusable")


class GitEnvironmentCredentialTests(unittest.TestCase):
    """How AL/X authenticates to push, and what she still cannot inherit.

    The environment these commands run under deliberately reads neither system
    nor global git configuration, which is what stops an inherited helper,
    editor or config injection reaching them. That also removed the machine's
    own credential helper, so she could read and commit but never push: with no
    helper at all, git has nothing to ask for a credential.

    One helper is therefore named here, for one host. No token is written, in
    code or configuration — the value names a program, and the GitHub CLI holds
    the secret in its own keyring and answers when asked.
    """

    def environment(self) -> dict[str, str]:
        from alx.providers.repository_authority import _git_environment

        return _git_environment()

    def resolved(self, *arguments: str) -> str:
        """What git resolves, under exactly the environment AL/X uses."""
        completed = subprocess.run(
            ["git", "config", *arguments],
            cwd=str(REPOSITORY_ROOT), env=self.environment(),
            capture_output=True, text=True,
        )
        return completed.stdout.strip()

    def test_inherited_helpers_are_still_unavailable(self) -> None:
        """System and global configuration are not read at all."""
        environment = self.environment()
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_the_generic_credential_helper_remains_reset(self) -> None:
        """An empty value discards every helper configuration would add."""
        self.assertEqual(self.resolved("--get-all", "credential.helper"), "")

    def test_github_resolves_to_the_command_line_helper(self) -> None:
        self.assertEqual(
            self.resolved("--get-urlmatch", "credential.helper",
                          "https://github.com"),
            "!gh auth git-credential",
        )

    def test_another_host_receives_no_helper(self) -> None:
        """Scoped to github.com: nothing else is answered for."""
        for host in ("https://gitlab.com", "https://example.test",
                     "https://github.com.evil.test"):
            with self.subTest(host=host):
                self.assertEqual(
                    self.resolved("--get-urlmatch", "credential.helper", host),
                    "",
                )

    def test_terminal_prompting_stays_disabled(self) -> None:
        """An unanswerable credential fails rather than waiting for a person."""
        environment = self.environment()
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_ASKPASS"], os.devnull)
        self.assertEqual(environment["SSH_ASKPASS"], os.devnull)

    def test_the_environment_carries_no_secret_material(self) -> None:
        """The helper names a program; it never carries a credential.

        A token written into configuration would reach every child process and
        any log that records an environment. What is here is the name of a
        command that knows how to ask.
        """
        environment = self.environment()
        rendered = " ".join(f"{name}={value}" for name, value in environment.items())
        for marker in ("ghp_", "gho_", "ghu_", "ghs_", "github_pat_",
                       "Authorization", "Bearer", "password", "token"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, rendered)

    def test_the_helper_is_a_command_rather_than_a_credential(self) -> None:
        from alx.providers.repository_authority import _SAFE_GIT_CONFIG

        helper = _SAFE_GIT_CONFIG["credential.https://github.com.helper"]
        # `!` is git's marker for "run this"; the value is an invocation.
        self.assertTrue(helper.startswith("!"))
        self.assertEqual(helper, "!gh auth git-credential")


if __name__ == "__main__":
    unittest.main()
