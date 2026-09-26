"""The Coding Agent's git authority: what it can do, and what it cannot.

A coding job used to hand back a dirty worktree, leaving AL/X to read a diff
and take the agent's word for which file belonged to which job. This gives the
job a bounded way to return a branch and a commit SHA instead, so a repair is
something Core can name rather than something it has to reconstruct.

The authority is narrow on purpose, and these tests are written around the two
things that could go wrong with it:

- **It could do too much.** Push, fetch, pull, merge, rebase, reset,
  checkout of an unrelated path, remote manipulation, stash and branch
  deletion are refused not by a denylist but by absence: the enumerated shapes
  in `coding_git._WRITE_SHAPES` are the only argv that can run. The tests
  assert the refusals directly against `git_write_permitted`, because a
  denylist test can pass while the list is incomplete and an enumeration test
  cannot.

- **It could commit somebody else's work.** A coding job runs in a worktree it
  does not own, and on 2026-09-11 one carried 109k of unrelated uncommitted
  work. Staging by `-A` would have swept that into the repair. Staging is by
  named path, the index is read back and compared against the authorised set,
  and one unauthorised entry refuses the commit outright rather than
  narrowing it.

Every test here runs against a real git repository. The behaviour being proved
is git's, not a mock's.
"""

from __future__ import annotations

import subprocess
import sys
import unittest.mock
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import (  # noqa: E402
    CodingCommit,
    CodingError,
    CodingRequest,
    GitWorkspaceState,
)
from alx.providers.coding_git import (  # noqa: E402
    assert_assigned_worktree,
    branch_name_permitted,
    commit_job_changes,
    deleted_paths,
    git_write_permitted,
    read_workspace_state,
)


def git(repository: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv], cwd=repository, check=True,
        capture_output=True, text=True,
    )
    return completed.stdout


def checkout_branch(repository: Path, branch: str) -> None:
    """Put a test repository on a branch, the way a job's worktree arrives.

    Plain git, deliberately. D-031 moved branch creation into the worktree
    allocator, and the commit tests below are about what may be committed once
    a job is on its branch — not about how it got there. Using the production
    allocator here would make every commit test depend on worktree allocation
    it is not exercising.
    """
    git(repository, "switch", "-q", "-c", branch)


class Worktree(unittest.TestCase):
    """One real repository with a committed baseline."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.test")
        git(self.root, "config", "user.name", "test")
        (self.root / "target.py").write_text("original\n")
        (self.root / "unrelated.py").write_text("original\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        self.base_sha = git(self.root, "rev-parse", "HEAD").strip()

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text)

    def status(self) -> str:
        return git(self.root, "status", "--porcelain")


class ReadingTheAssignedWorktree(Worktree):
    """Before coding, a job can prove where it is and what it inherited."""

    def test_head_and_branch_are_read_from_git(self) -> None:
        state = read_workspace_state(self.root)
        self.assertIsInstance(state, GitWorkspaceState)
        self.assertEqual(state.branch, "main")
        self.assertEqual(state.head_sha, self.base_sha)
        self.assertFalse(state.detached)

    def test_a_clean_worktree_reports_itself_clean(self) -> None:
        state = read_workspace_state(self.root)
        self.assertTrue(state.clean)
        self.assertEqual(state.inherited_dirty, ())

    def test_inherited_dirt_is_reported_before_the_job_runs(self) -> None:
        """A job must be able to prove what it did not do."""
        self.write("unrelated.py", "somebody else was here\n")
        state = read_workspace_state(self.root)
        self.assertFalse(state.clean)
        self.assertIn("unrelated.py", state.inherited_dirty)

    def test_an_untracked_file_counts_as_inherited_dirt(self) -> None:
        (self.root / "left_behind.txt").write_text("x\n")
        state = read_workspace_state(self.root)
        self.assertIn("left_behind.txt", state.inherited_dirty)
        self.assertFalse(state.clean)

    def test_a_detached_head_is_reported_rather_than_guessed(self) -> None:
        git(self.root, "checkout", "-q", "--detach", "HEAD")
        state = read_workspace_state(self.root)
        self.assertTrue(state.detached)
        self.assertEqual(state.branch, "")
        self.assertEqual(state.head_sha, self.base_sha)

    def test_a_directory_that_is_not_a_repository_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(CodingError) as caught:
                read_workspace_state(Path(plain))
            self.assertEqual(caught.exception.code, "git_unavailable")

    def test_a_subdirectory_is_not_the_assigned_worktree(self) -> None:
        """Running from inside would silently operate on the enclosing repo."""
        nested = self.root / "src"
        nested.mkdir()
        with self.assertRaises(CodingError) as caught:
            assert_assigned_worktree(nested)
        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"],
            "worktree_is_not_repository_root",
        )


class BranchNamesTheCapabilityMayUse(Worktree):
    """The narrow branch grammar and creation authority."""

    def test_only_feature_branch_creation_is_an_available_switch_shape(self) -> None:
        self.assertFalse(git_write_permitted(["git", "switch", "main"]))
        self.assertTrue(
            git_write_permitted(
                ["git", "switch", "-c", "repair/x", self.base_sha]
            )
        )
        self.assertFalse(
            git_write_permitted(["git", "switch", "-c", "repair/x"])
        )

    def test_a_branch_name_reaching_another_ref_namespace_is_refused(self) -> None:
        for name in ("refs/heads/main", "HEAD", "main@{1}", "a/../b"):
            self.assertFalse(branch_name_permitted(name), name)

    def test_a_branch_name_that_could_be_read_as_a_flag_is_refused(self) -> None:
        for name in ("--force", "-D", "--all"):
            self.assertFalse(branch_name_permitted(name), name)

    def test_ordinary_repair_branch_names_are_permitted(self) -> None:
        for name in ("repair/target", "fix-123", "feat/ca-git-workspace"):
            self.assertTrue(branch_name_permitted(name), name)


class CommittingOnlyJobOwnedChanges(Worktree):
    """The property the whole capability exists to guarantee."""

    def test_a_job_owned_change_is_committed_and_reported(self) -> None:
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")

        commit = commit_job_changes(
            self.root, "repair/target", "repair target", ("target.py",)
        )

        self.assertIsInstance(commit, CodingCommit)
        self.assertEqual(commit.branch, "repair/target")
        self.assertEqual(commit.committed_files, ("target.py",))
        self.assertTrue(commit.worktree_clean)
        # The SHA is git's, not a prediction: it is what HEAD actually holds.
        self.assertEqual(
            commit.commit_sha, git(self.root, "rev-parse", "HEAD").strip()
        )
        self.assertNotEqual(commit.commit_sha, self.base_sha)
        self.assertEqual(
            git(self.root, "log", "-1", "--pretty=%s").strip(), "repair target"
        )

    def test_an_inherited_dirty_file_is_not_staged(self) -> None:
        """The live defect: somebody else's work swept into the repair."""
        self.write("unrelated.py", "somebody else was here\n")
        baseline = read_workspace_state(self.root)
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")

        commit = commit_job_changes(
            self.root, "repair/target", "repair target", ("target.py",),
            baseline.inherited_dirty,
        )

        self.assertEqual(commit.committed_files, ("target.py",))
        self.assertNotIn(
            "unrelated.py",
            git(self.root, "show", "--name-only", "--pretty=", "HEAD"),
        )
        # And it is still there, still dirty, exactly as it was found.
        self.assertEqual(
            (self.root / "unrelated.py").read_text(), "somebody else was here\n"
        )
        self.assertIn("unrelated.py", self.status())
        self.assertFalse(commit.worktree_clean)

    def test_an_unrelated_staged_file_refuses_the_commit(self) -> None:
        """Fail closed: refuse entirely rather than commit somebody's work."""
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        self.write("unrelated.py", "somebody else was here\n")
        # Pretend the job's file set wrongly claims a file it does not own by
        # staging it out of band, the way a concurrent editor would.
        (self.root / "sneaked.py").write_text("not this job's\n")
        git(self.root, "add", "sneaked.py")

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair target", ("target.py",),
                ("unrelated.py",),
            )

        self.assertIn(
            caught.exception.code,
            ("unrelated_changes_staged", "git_refused"),
        )
        # No commit exists, and the unrelated file was neither committed nor
        # reverted on disk.
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), self.base_sha)
        self.assertEqual((self.root / "sneaked.py").read_text(), "not this job's\n")

    def test_a_commit_with_no_job_owned_changes_is_refused(self) -> None:
        checkout_branch(self.root, "repair/target")
        with self.assertRaises(CodingError) as caught:
            commit_job_changes(self.root, "repair/target", "nothing", ())
        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"], "no_job_owned_changes"
        )

    def test_committing_on_a_branch_that_is_not_active_is_refused(self) -> None:
        """The branch argument must describe the worktree, not redirect it."""
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/other", "repair target", ("target.py",)
            )
        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(caught.exception.details["reason_code"], "branch_not_active")

    def test_a_path_outside_the_worktree_cannot_be_staged(self) -> None:
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        for escape in ("../outside.py", "/etc/passwd"):
            with self.assertRaises(CodingError, msg=escape):
                commit_job_changes(
                    self.root, "repair/target", "escape", ("target.py", escape)
                )
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), self.base_sha)

    def test_a_blocked_path_cannot_be_staged(self) -> None:
        checkout_branch(self.root, "repair/target")
        (self.root / "secrets.txt").write_text("token\n")
        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "leak", ("secrets.txt",),
                (), ("secrets.txt",),
            )
        self.assertEqual(caught.exception.code, "path_not_permitted")

    def test_git_metadata_cannot_be_staged(self) -> None:
        checkout_branch(self.root, "repair/target")
        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "metadata", (".git/config",)
            )
        self.assertEqual(caught.exception.code, "path_not_permitted")

    def test_a_blank_commit_message_is_refused(self) -> None:
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "   ", ("target.py",)
            )
        self.assertEqual(caught.exception.code, "git_refused")

    def test_cleanliness_is_reported_from_the_worktree_after_committing(self) -> None:
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        clean = commit_job_changes(
            self.root, "repair/target", "repair", ("target.py",)
        )
        self.assertTrue(clean.worktree_clean)
        self.assertEqual(self.status().strip(), "")


class AuthorisationReadsTheWholeTruth(Worktree):
    """Findings 1 and 2 from the 2026-09-12 authority-boundary review.

    Both had the same shape: the check consulted something that was not the
    whole truth, and the gap between what it saw and what git held was where
    an unauthorised file fitted.
    """

    def test_a_large_index_is_refused_rather_than_compared_in_part(self) -> None:
        """Finding 1: 16,000 characters of a 2,000-file index is 334 paths.

        The other 1,666 were invisible to the authorisation check and would
        have been committed unexamined. Structural listings are now read whole
        and bounded by entry count, where exceeding the bound fails closed.
        """
        from alx.providers.coding_git import _staged_paths

        for index in range(400):
            name = f"padding_with_a_deliberately_long_file_name_{index:04d}.py"
            (self.root / name).write_text("x\n")
        git(self.root, "add", "-A")

        staged = _staged_paths(self.root)
        # Every one of them, not the prefix that fitted in a character bound.
        self.assertEqual(len(staged), 400)
        self.assertIn("padding_with_a_deliberately_long_file_name_0399.py", staged)

    def test_an_oversized_listing_fails_closed(self) -> None:
        """Bounding by entries still refuses; it never shortens the answer."""
        from alx.providers import coding_git

        with unittest.mock.patch.object(coding_git, "MAX_INSPECTED_ENTRIES", 2):
            self.write("target.py", "a\n")
            self.write("unrelated.py", "b\n")
            (self.root / "third.py").write_text("c\n")
            with self.assertRaises(CodingError) as caught:
                read_workspace_state(self.root)
        self.assertEqual(caught.exception.code, "git_refused")

    def test_a_pre_commit_hook_cannot_add_a_file_to_the_commit(self) -> None:
        """Finding 2: a hook staged a file after authorisation and it committed.

        Worse than the extra file, `committed_files` reported the pre-hook
        listing, so the evidence returned to Core was false. Hooks are now
        disabled for every command this capability runs.
        """
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "echo hooked > sneaked.py\n"
            "git add sneaked.py\n"
            "exit 0\n"
        )
        hook.chmod(0o755)
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")

        commit = commit_job_changes(
            self.root, "repair/target", "repair target", ("target.py",)
        )

        self.assertEqual(commit.committed_files, ("target.py",))
        self.assertNotIn(
            "sneaked.py",
            git(self.root, "show", "--name-only", "--pretty=", "HEAD"),
        )

    def test_a_clean_filter_cannot_rewrite_committed_content(self) -> None:
        """Found while attacking question 2 of the 2026-09-12 re-review.

        A `.gitattributes` clean filter runs arbitrary repository code during
        `git add` and replaces the bytes entering the index. Reproduced: the
        commit read "TAMPERED change" while the worktree read "job change",
        and a filter body of `sh -c ...` executed code outside the worktree.
        The file set stayed authorised throughout, which is the point —
        path-level authorisation says nothing about content.
        """
        (self.root / ".gitattributes").write_text("target.py filter=rewrite\n")
        git(self.root, "config", "filter.rewrite.clean", "sed s/repaired/TAMPERED/")
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair target", ("target.py",)
            )

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"],
            "attribute_filter_would_rewrite_content",
        )
        # Nothing was committed, so no tampered content reached history.
        self.assertEqual(
            git(self.root, "rev-parse", "HEAD").strip(),
            git(self.root, "rev-parse", "main").strip(),
        )

    def test_an_unfiltered_path_is_unaffected_by_the_check(self) -> None:
        """The check refuses filtered paths, not every repository with attributes."""
        (self.root / ".gitattributes").write_text("*.md text\n")
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        commit = commit_job_changes(
            self.root, "repair/target", "repair target", ("target.py",)
        )
        self.assertEqual(commit.committed_files, ("target.py",))

    def test_a_commit_racing_with_another_writer_is_detected(self) -> None:
        """The residual window: staging between readback and commit.

        Detected, not prevented. The commit exists when this fires, and it is
        named rather than hidden: undoing it means moving a ref, which is
        history rewriting D-029 withholds.
        """
        from alx.providers import coding_git

        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        self.write("unrelated.py", "somebody else was here\n")

        real = coding_git._staged_paths
        calls = {"n": 0}

        def racing(worktree):
            result = real(worktree)
            calls["n"] += 1
            if calls["n"] == 2:
                git(worktree, "add", "unrelated.py")
            return result

        with unittest.mock.patch.object(coding_git, "_staged_paths", racing):
            with self.assertRaises(CodingError) as caught:
                commit_job_changes(
                    self.root, "repair/target", "repair target", ("target.py",)
                )

        self.assertEqual(caught.exception.code, "unrelated_changes_staged")
        self.assertEqual(
            caught.exception.details["reason_code"],
            "commit_contains_unauthorised_paths",
        )
        # The SHA is reported so AL/X can act on a commit that does exist.
        self.assertTrue(caught.exception.details["commit_sha"])

    def test_a_configured_signing_program_is_not_executed(self) -> None:
        """`gpg.program` launches an arbitrary binary during the commit.

        Reproduced on 2026-09-12: a repository-configured program ran despite
        the closed argv allowlist, because the allowlist bounds which git
        subcommand runs and not what that subcommand's configuration makes it
        launch. Signing is not part of this authority and a coding job has no
        key, so it is disabled rather than left to host configuration.
        """
        marker = self.root / "signing_program_ran"
        program = self.root / "fake_signer"
        program.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        program.chmod(0o755)
        git(self.root, "config", "commit.gpgsign", "true")
        git(self.root, "config", "gpg.program", str(program))
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")

        commit = commit_job_changes(
            self.root, "repair/target", "repair target", ("target.py",)
        )

        self.assertFalse(marker.exists(), "a signing program must not run")
        # And the commit succeeded, where the misconfiguration used to break it.
        self.assertEqual(commit.committed_files, ("target.py",))

    def test_the_committed_tree_is_verified_not_assumed(self) -> None:
        """Defence in depth: the report is read out of the commit itself.

        Hooks are disabled, so this should never fire. It exists because the
        alternative to checking is reporting a file list that a hook, a git
        version or a configuration could have made untrue, and a false
        `committed_files` is worse than a refusal.
        """
        import inspect

        from alx.providers.coding_git import (
            _verify_committed_tree,
            commit_job_changes,
        )

        source = inspect.getsource(_verify_committed_tree)
        self.assertIn("_committed_paths(root)", source)
        self.assertIn("commit_contains_unauthorised_paths", source)
        # And the phase is actually reached: a helper nothing calls proves
        # nothing. Asserted here because this check is the last thing standing
        # between a bad commit and a CodingCommit that vouches for it.
        self.assertIn(
            "_verify_committed_tree(", inspect.getsource(commit_job_changes)
        )


class NarrowedRefusalsUnderD029V1(Worktree):
    """The 2026-09-13 findings, closed by refusing rather than by cleverness.

    Each of these is a state D-029 V1 does not authorise. The design choice is
    fail-closed simplicity: rather than teach the authorisation set to model
    rename pairs, symlinks, ignore rules and partial directories, the
    conditions themselves are refused and the job returns to AL/X. A refusal
    she can act on beats a commit nobody can verify.

    Every test also asserts the index and worktree are left exactly as found.
    """

    def test_a_directory_is_not_a_valid_commit_input(self) -> None:
        """D-029 V1 takes concrete files. Directories are refused outright.

        `git add <dir>` discovers files by walking, and the set it discovers is
        not the set the job was authorised for. Supporting it meant expansion,
        and expansion meant deciding about symlinks, ignored files, nested
        directories and a bound counted on the wrong side of the walk — four
        defects from one convenience, all found in the PR #31 review.

        The job already knows which files it wrote, and `git status -uall`
        names every untracked file rather than collapsing a new directory, so
        the concrete paths are available without this module discovering
        anything.
        """
        checkout_branch(self.root, "repair/target")
        (self.root / "newdir").mkdir()
        (self.root / "newdir" / "a.py").write_text("a\n")

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair", ("newdir/",)
            )

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"], "path_is_not_a_regular_file"
        )

    def test_the_files_inside_a_new_directory_commit_when_named(self) -> None:
        """The capability is not lost, only the directory shorthand."""
        checkout_branch(self.root, "repair/target")
        (self.root / "newdir" / "nested").mkdir(parents=True)
        (self.root / "newdir" / "a.py").write_text("a\n")
        (self.root / "newdir" / "nested" / "b.py").write_text("b\n")

        commit = commit_job_changes(
            self.root, "repair/target", "add a package",
            ("newdir/a.py", "newdir/nested/b.py"),
        )

        self.assertEqual(
            commit.committed_files, ("newdir/a.py", "newdir/nested/b.py")
        )

    def test_status_names_untracked_files_individually(self) -> None:
        """What makes concrete-files-only workable: git does the naming.

        Without `-uall` a wholly untracked directory is one `dir/` entry, and
        the job's derived file set would name something D-029 must refuse.
        """
        (self.root / "newdir" / "nested").mkdir(parents=True)
        (self.root / "newdir" / "a.py").write_text("a\n")
        (self.root / "newdir" / "nested" / "b.py").write_text("b\n")

        dirt = read_workspace_state(self.root).inherited_dirty

        self.assertIn("newdir/a.py", dirt)
        self.assertIn("newdir/nested/b.py", dirt)
        self.assertNotIn("newdir/", dirt)

    def test_a_bare_file_count_is_bounded(self) -> None:
        from alx.contracts.coding import MAX_STAGED_FILES

        checkout_branch(self.root, "repair/target")
        names = []
        for index in range(MAX_STAGED_FILES + 5):
            name = f"f{index:03d}.py"
            (self.root / name).write_text("x\n")
            names.append(name)

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair", tuple(names)
            )

        self.assertEqual(caught.exception.details["reason_code"], "too_many_files")

    def test_a_filename_containing_glob_characters_stages_literally(self) -> None:
        """Paths are exact filenames, never patterns.

        Without `GIT_LITERAL_PATHSPECS`, `*`, `?` and bracket expressions in a
        filename are read as globs. A file legitimately named `weird[1].py`
        also matched a sibling `weird1.py` the job did not own: the readback
        caught and refused it, which is correct but penalises a job that did
        nothing wrong. Found in the PR #31 review on 2026-09-13.
        """
        checkout_branch(self.root, "repair/target")
        (self.root / "weird[1].py").write_text("owned\n")
        (self.root / "weird1.py").write_text("not this job's\n")

        commit = commit_job_changes(
            self.root, "repair/target", "repair", ("weird[1].py",)
        )

        self.assertEqual(commit.committed_files, ("weird[1].py",))
        # The sibling the glob would have matched is untouched and untracked.
        self.assertIn("weird1.py", self.status())
        self.assertEqual((self.root / "weird1.py").read_text(), "not this job's\n")

    def test_a_deleted_file_cannot_be_committed_in_v1(self) -> None:
        """A known V1 bound, refused honestly rather than silently omitted.

        The invariant requires every input to be one concrete regular file
        that exists, so a job that deletes a file names a path that is gone.
        It refuses rather than committing the rest and reporting success —
        a partial commit presented as complete is the defect this capability
        exists to prevent — but it does mean a repair that removes a
        superseded module cannot be committed by D-029 V1.
        """
        checkout_branch(self.root, "repair/target")
        (self.root / "unrelated.py").unlink()
        self.write("target.py", "repaired\n")

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "remove and repair",
                ("unrelated.py", "target.py"),
            )

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"], "path_is_not_a_regular_file"
        )
        self.assertEqual(caught.exception.details["path"], "unrelated.py")

    def test_a_staged_rename_refuses_the_commit(self) -> None:
        """The serious one: a repair commit that deleted an unowned file.

        Porcelain reports a rename as destination-plus-source and every
        listing this authorises from shows only the destination — on the index
        *and* on the committed tree. A job editing an inherited rename
        destination was authorised on the destination alone, and its commit
        deleted the source. The post-commit verification structurally could
        not see it.
        """
        git(self.root, "mv", "unrelated.py", "renamed.py")
        (self.root / "renamed.py").write_text("job edit\n")
        checkout_branch(self.root, "repair/target")
        before = self.status()

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair", ("renamed.py",)
            )

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"],
            "index_contains_staged_rename",
        )
        # Nothing was committed and the staged rename is exactly as found.
        self.assertEqual(self.status(), before)
        self.assertIn("renamed.py", self.status())

    def test_a_job_owned_symlink_refuses_rather_than_vanishing(self) -> None:
        """It used to be dropped silently while the result reported success."""
        checkout_branch(self.root, "repair/target")
        (self.root / "alias.py").symlink_to("target.py")

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair", ("alias.py",)
            )

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(caught.exception.details["reason_code"], "path_is_symlink")

    def test_rollback_never_unstages_another_writers_work(self) -> None:
        """The rollback did the very thing the pre-flight check refuses to do.

        Only staging this operation itself introduced is reverted. A path
        outside the authorised set is another writer's and is left exactly as
        found, still staged.
        """
        from alx.providers import coding_git

        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        self.write("unrelated.py", "somebody else was here\n")

        real = coding_git._run

        def racing(worktree, argv, **keywords):
            result = real(worktree, argv, **keywords)
            if argv[:2] == ["git", "add"]:
                git(worktree, "add", "unrelated.py")
            return result

        with unittest.mock.patch.object(coding_git, "_run", racing):
            with self.assertRaises(CodingError) as caught:
                commit_job_changes(
                    self.root, "repair/target", "repair", ("target.py",)
                )

        self.assertEqual(caught.exception.code, "unrelated_changes_staged")
        status = self.status()
        # Theirs is still staged; ours was taken back out of the index.
        self.assertIn("M  unrelated.py", status)
        self.assertIn(" M target.py", status)


class ExplicitJobOwnedDeletions(Worktree):
    """The second and only other input kind D-029 V1 accepts.

    A capability that can create and modify but not delete is too constrained
    for repair work, and Law 0 requires superseded paths to be deleted — so a
    Law 0 cleanup was exactly the job the previous version could not commit.

    The amendment is deliberately narrow: a deletion is authorised only when
    the job names it *and* git reports that exact tracked path deleted. No
    directory semantics, no rename inference, and nothing is discovered — a
    path missing from disk that the job did not name is not a deletion here.
    """

    def test_a_job_owned_deletion_commits(self) -> None:
        inherited = deleted_paths(self.root)
        checkout_branch(self.root, "repair/target")
        (self.root / "unrelated.py").unlink()

        commit = commit_job_changes(
            self.root, "repair/target", "remove superseded module", (),
            (), (), ("unrelated.py",), inherited,
        )

        self.assertEqual(commit.committed_files, ("unrelated.py",))
        self.assertIn(
            "D\tunrelated.py",
            git(self.root, "show", "--name-status", "--pretty=", "HEAD"),
        )
        # Gone from the tree, not merely named in the commit.
        self.assertNotIn(
            "unrelated.py", git(self.root, "ls-tree", "--name-only", "HEAD")
        )

    def test_a_modification_and_a_deletion_commit_together(self) -> None:
        inherited = deleted_paths(self.root)
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        (self.root / "unrelated.py").unlink()

        commit = commit_job_changes(
            self.root, "repair/target", "repair and remove", ("target.py",),
            (), (), ("unrelated.py",), inherited,
        )

        self.assertEqual(
            sorted(commit.committed_files), ["target.py", "unrelated.py"]
        )
        shown = git(self.root, "show", "--name-status", "--pretty=", "HEAD")
        self.assertIn("M\ttarget.py", shown)
        self.assertIn("D\tunrelated.py", shown)
        self.assertTrue(commit.worktree_clean)

    def test_an_inherited_deletion_is_refused(self) -> None:
        """Absent before the job started is never this job's to commit."""
        (self.root / "unrelated.py").unlink()
        inherited = deleted_paths(self.root)
        self.assertIn("unrelated.py", inherited)
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair", ("target.py",),
                (), (), ("unrelated.py",), inherited,
            )

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"], "deletion_is_inherited"
        )
        self.assertEqual(
            git(self.root, "rev-parse", "HEAD").strip(),
            git(self.root, "rev-parse", "main").strip(),
        )

    def test_a_path_not_present_at_baseline_is_refused(self) -> None:
        """Git's own account is the proof, not the job's claim."""
        inherited = deleted_paths(self.root)
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair", ("target.py",),
                (), (), ("never_existed.py",), inherited,
            )

        self.assertEqual(
            caught.exception.details["reason_code"],
            "path_is_not_a_deleted_tracked_file",
        )

    def test_a_still_present_file_cannot_be_claimed_as_deleted(self) -> None:
        inherited = deleted_paths(self.root)
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "repair", ("target.py",),
                (), (), ("unrelated.py",), inherited,
            )

        self.assertEqual(
            caught.exception.details["reason_code"],
            "path_is_not_a_deleted_tracked_file",
        )
        # And it is still there, tracked and unmodified.
        self.assertTrue((self.root / "unrelated.py").is_file())

    def test_a_directory_is_not_a_valid_deletion_input(self) -> None:
        """No directory deletion semantics: git never reports `dir/` deleted."""
        inherited = deleted_paths(self.root)
        checkout_branch(self.root, "repair/target")
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "a.py").write_text("a\n")
        git(self.root, "add", "pkg/a.py")
        git(self.root, "commit", "-qm", "add package")
        (self.root / "pkg" / "a.py").unlink()
        (self.root / "pkg").rmdir()

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "remove package", (),
                (), (), ("pkg/",), inherited,
            )

        self.assertEqual(
            caught.exception.details["reason_code"],
            "path_is_not_a_deleted_tracked_file",
        )

    def test_an_unrelated_deletion_cannot_enter_the_commit(self) -> None:
        """Somebody else's removal is not swept in with the job's own."""
        inherited = deleted_paths(self.root)
        checkout_branch(self.root, "repair/target")
        (self.root / "extra.py").write_text("x\n")
        git(self.root, "add", "extra.py")
        git(self.root, "commit", "-qm", "add extra")
        self.write("target.py", "repaired\n")
        # A concurrent writer removes a file this job never named.
        (self.root / "extra.py").unlink()

        commit = commit_job_changes(
            self.root, "repair/target", "repair", ("target.py",),
            (), (), (), inherited,
        )

        self.assertEqual(commit.committed_files, ("target.py",))
        # Theirs is still tracked in the commit and still missing on disk.
        self.assertIn(
            "extra.py", git(self.root, "ls-tree", "--name-only", "HEAD")
        )
        self.assertIn("extra.py", self.status())

    def test_a_blocked_path_cannot_be_deleted(self) -> None:
        inherited = deleted_paths(self.root)
        checkout_branch(self.root, "repair/target")
        (self.root / "unrelated.py").unlink()

        with self.assertRaises(CodingError) as caught:
            commit_job_changes(
                self.root, "repair/target", "remove", (),
                (), ("unrelated.py",), ("unrelated.py",), inherited,
            )

        self.assertEqual(caught.exception.code, "path_not_permitted")

    def test_a_job_with_neither_kind_is_refused(self) -> None:
        checkout_branch(self.root, "repair/target")
        with self.assertRaises(CodingError) as caught:
            commit_job_changes(self.root, "repair/target", "nothing", ())
        self.assertEqual(
            caught.exception.details["reason_code"], "no_job_owned_changes"
        )


class ForbiddenOperationsCannotBeExpressed(unittest.TestCase):
    """Refusal by absence from the enumeration, not by a denylist.

    A denylist test can pass while the list is incomplete. These assert against
    the enumerated allowlist itself, so a new forbidden operation is refused
    the moment it is not added rather than the moment somebody remembers it.
    """

    FORBIDDEN = (
        ["git", "push"],
        ["git", "push", "origin", "main"],
        ["git", "push", "--force"],
        ["git", "fetch"],
        ["git", "fetch", "origin"],
        ["git", "pull"],
        ["git", "pull", "--rebase"],
        ["git", "merge", "main"],
        ["git", "merge", "--abort"],
        ["git", "rebase", "main"],
        ["git", "rebase", "--continue"],
        ["git", "reset", "--hard"],
        ["git", "reset", "--hard", "HEAD~1"],
        ["git", "checkout", "main"],
        ["git", "checkout", "--", "unrelated.py"],
        ["git", "checkout", "-f"],
        ["git", "remote", "add", "origin", "https://example.invalid/x.git"],
        ["git", "remote", "set-url", "origin", "https://example.invalid/x.git"],
        ["git", "remote", "remove", "origin"],
        ["git", "stash"],
        ["git", "stash", "pop"],
        ["git", "stash", "drop", "stash@{0}"],
        ["git", "stash", "list"],
        ["git", "branch", "-D", "main"],
        ["git", "branch", "-d", "main"],
        ["git", "branch", "-m", "main", "other"],
        ["git", "tag", "v1"],
        ["git", "clean", "-fdx"],
        ["git", "rm", "-r", "src"],
        ["git", "cherry-pick", "HEAD"],
        ["git", "revert", "HEAD"],
        ["git", "filter-branch"],
        ["git", "update-ref", "refs/heads/main", "HEAD"],
        ["git", "config", "user.email", "x@y.z"],
        ["git", "gc"],
        ["git", "worktree", "add", "/tmp/elsewhere"],
        ["git", "submodule", "update", "--init"],
    )

    def test_no_forbidden_operation_is_permitted(self) -> None:
        for argv in self.FORBIDDEN:
            self.assertFalse(
                git_write_permitted(argv), f"{' '.join(argv)} must be impossible"
            )

    def test_amending_and_rewriting_are_not_permitted(self) -> None:
        """Rewriting history needs AL/X's explicit authority, which is absent."""
        for argv in (
            ["git", "commit", "--amend"],
            ["git", "commit", "--amend", "-m", "rewritten"],
            ["git", "commit", "--quiet", "--amend", "-m", "rewritten"],
            ["git", "commit", "--quiet", "-m", "x", "--amend"],
            ["git", "commit", "--no-verify", "-m", "x"],
        ):
            self.assertFalse(git_write_permitted(argv), " ".join(argv))

    def test_staging_the_whole_tree_is_not_permitted(self) -> None:
        """`-A`, `-u` and `.` are how inherited dirt gets swept into a repair."""
        for argv in (
            ["git", "add", "-A"],
            ["git", "add", "-u"],
            ["git", "add", "."],
            ["git", "add", "--all"],
            ["git", "add", "-A", "--", "."],
        ):
            self.assertFalse(git_write_permitted(argv), " ".join(argv))

    def test_the_worktree_binding_cannot_be_redirected(self) -> None:
        """A global option before the subcommand would move the repository."""
        for argv in (
            ["git", "-C", "/elsewhere", "status", "--porcelain=v1", "-z"],
            ["git", "--git-dir=/elsewhere/.git", "rev-parse", "HEAD"],
            ["git", "--work-tree=/elsewhere", "add", "--", "target.py"],
            ["git", "-c", "core.hooksPath=/tmp", "commit", "--quiet", "-m", "x"],
            ["git", "--exec-path=/tmp", "status", "--porcelain=v1", "-z"],
        ):
            self.assertFalse(git_write_permitted(argv), " ".join(argv))

    def test_a_non_git_executable_is_not_permitted(self) -> None:
        for argv in (
            ["sh", "-c", "git push"],
            ["bash", "-c", "git push"],
            ["/usr/bin/git", "status", "--porcelain=v1", "-z"],
            ["gh", "pr", "create"],
        ):
            self.assertFalse(git_write_permitted(argv), " ".join(argv))

    def test_the_enumerated_operations_are_permitted(self) -> None:
        """The other half: what the capability is actually for still works."""
        for argv in (
            ["git", "rev-parse", "HEAD"],
            ["git", "status", "--porcelain=v1", "-z", "-uall"],
            ["git", "diff", "--cached", "--name-only", "-z"],
            ["git", "add", "--", "target.py"],
            ["git", "add", "--", "a.py", "b.py"],
            ["git", "commit", "--quiet", "-m", "repair target"],
        ):
            self.assertTrue(git_write_permitted(argv), " ".join(argv))

    def test_the_index_rollback_reset_stays_inside_its_approved_scope(self) -> None:
        """D-029 approves one reset shape, and only as an index rollback.

        Friedl's approval names four conditions: named job paths only, no
        `--hard`/`--soft`/`--mixed`, no commit or ref target, and no exposure
        as general reset authority. Each is asserted here against the
        allowlist, so the record's claim that they hold by construction is
        something the tests prove rather than something the prose asserts.
        """
        for argv in (
            ["git", "reset", "--hard"],
            ["git", "reset", "--soft", "HEAD~1"],
            ["git", "reset", "--mixed"],
            ["git", "reset", "--quiet", "--hard"],
            ["git", "reset", "--quiet", "--soft", "--", "target.py"],
            # A ref target, with and without the separator.
            ["git", "reset", "--quiet", "HEAD"],
            ["git", "reset", "--quiet", "--", "HEAD"],
            ["git", "reset", "--quiet", "--", "HEAD~1"],
            ["git", "reset", "--quiet", "--", "refs/heads/main"],
            # General reset authority, which CA never receives.
            ["git", "reset"],
            ["git", "reset", "--quiet"],
        ):
            self.assertFalse(git_write_permitted(argv), " ".join(argv))

        # The one approved form: named paths, no ref, index only.
        self.assertTrue(
            git_write_permitted(["git", "reset", "--quiet", "--", "target.py"])
        )

    def test_a_ref_shaped_pathspec_is_refused_everywhere(self) -> None:
        """Not only for reset: `add` is held to the same reading."""
        for argv in (
            ["git", "add", "--", "HEAD"],
            ["git", "add", "--", "refs/heads/main"],
            ["git", "add", "--", "main@{1}"],
            ["git", "add", "--", "a..b"],
            ["git", "add", "--", ":/message"],
        ):
            self.assertFalse(git_write_permitted(argv), " ".join(argv))

    def test_the_enumeration_has_not_quietly_grown(self) -> None:
        """Adding authority must be a visible change, not an accident.

        The count is asserted rather than the contents so this fails on any
        addition and the author has to state, here, what the new shape is for.
        It has already earned that: adding the post-commit readback below
        failed this test rather than slipping in.

        The shapes, and why each exists:
        three reads of where the worktree is (`rev-parse` x2, `symbolic-ref`);
        two reads of what is changed (`status`, `diff --cached`);
        one read of what a commit contains (`show`), added 2026-09-12 to verify
        the committed tree against the authorised set rather than trusting the
        index snapshot; one read of which attribute filter applies to a path
        (`check-attr`), added 2026-09-12 to refuse a path whose content a clean
        filter would rewrite; one read of whether a path is ignored
        (`check-ignore`) and one of whether the index holds a rename
        (`diff --cached --name-status`), both added 2026-09-13 so those states
        are refused rather than committed as a subset; one read of whether a
        path is still tracked (`ls-files`), added 2026-09-14 to prove an
        authorised deletion is actually absent from the committed tree; one branch creation
        (`switch -c`); one staging (`add`); one index rollback (`reset`); one
        commit.

        The plain `switch` shape was removed on 2026-09-12: once an existing
        branch name is refused, nothing called it, and a dead shape is granted
        authority nobody uses.

        D-033 removes linked-worktree allocation and release. The common git
        directory read now locates the repository-scoped exclusivity lock,
        and `switch -c <feature> <verified-main-sha>` is the sole branch
        creation shape.
        """
        from alx.providers.coding_git import _WRITE_SHAPES

        self.assertEqual(len(_WRITE_SHAPES), 16)
        subcommands = {prefix[0] for prefix in _WRITE_SHAPES}
        self.assertEqual(
            subcommands,
            {"rev-parse", "symbolic-ref", "status", "diff", "show",
             "check-attr", "check-ignore", "ls-files", "add",
             "reset", "commit", "switch"},
        )
        self.assertIn(("switch", "-c"), _WRITE_SHAPES)
        self.assertFalse(
            any(prefix[0] in {"worktree", "cat-file"} for prefix in _WRITE_SHAPES)
        )
        # Every read-shaped addition must stay a read. `check-attr` takes
        # paths because it is asked about specific paths, but it only reports;
        # the rest may take neither a value nor a path, so none can be pointed
        # somewhere by an argument.
        for prefix, remainder in _WRITE_SHAPES.items():
            if prefix[0] in ("rev-parse", "symbolic-ref", "status", "diff", "show"):
                self.assertEqual(remainder, "none", " ".join(prefix))


class OperatingOutsideTheAssignedWorktree(Worktree):
    """Every entry point binds to the assigned worktree and nowhere else."""

    def setUp(self) -> None:
        super().setUp()
        self.other = tempfile.TemporaryDirectory()
        self.addCleanup(self.other.cleanup)
        self.elsewhere = Path(self.other.name).resolve()
        git(self.elsewhere, "init", "-q", "-b", "main")
        git(self.elsewhere, "config", "user.email", "other@example.test")
        git(self.elsewhere, "config", "user.name", "other")
        (self.elsewhere / "theirs.py").write_text("theirs\n")
        git(self.elsewhere, "add", "-A")
        git(self.elsewhere, "commit", "-qm", "their base")
        self.their_sha = git(self.elsewhere, "rev-parse", "HEAD").strip()

    def test_a_commit_cannot_reach_another_repository_through_a_path(self) -> None:
        checkout_branch(self.root, "repair/target")
        self.write("target.py", "repaired\n")
        with self.assertRaises(CodingError):
            commit_job_changes(
                self.root, "repair/target", "reach out",
                ("target.py", str(self.elsewhere / "theirs.py")),
            )
        # The other repository is untouched: same HEAD, still clean.
        self.assertEqual(
            git(self.elsewhere, "rev-parse", "HEAD").strip(), self.their_sha
        )
        self.assertEqual(git(self.elsewhere, "status", "--porcelain").strip(), "")

    def test_a_symlink_out_of_the_worktree_cannot_be_staged(self) -> None:
        checkout_branch(self.root, "repair/target")
        (self.root / "escape").symlink_to(self.elsewhere / "theirs.py")
        with self.assertRaises(CodingError):
            commit_job_changes(
                self.root, "repair/target", "escape", ("escape",)
            )


class CommitEvidenceReachesTheOutcome(unittest.TestCase):
    """A successful coding outcome can now return a branch and a commit SHA."""

    def test_a_request_carries_the_branch_and_message_core_chose(self) -> None:
        """Naming the repair is AL/X's judgment, so it arrives as an argument."""
        request = CodingRequest(
            "fix the thing", ".",
            repair_branch="repair/target", commit_message="repair target",
        )
        self.assertEqual(request.repair_branch, "repair/target")
        self.assertEqual(request.commit_message, "repair target")

    def test_a_commit_message_without_a_branch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodingRequest("fix", ".", commit_message="repair target")

    def test_a_request_without_either_still_works(self) -> None:
        """The capability as it was: edit the worktree, return a diff."""
        request = CodingRequest("fix the thing", ".")
        self.assertEqual(request.repair_branch, "")
        self.assertEqual(request.commit_message, "")

    def test_the_outcome_carries_branch_and_sha_to_core(self) -> None:
        from datetime import UTC, datetime

        from alx.contracts.coding import CodingOutcome

        outcome = CodingOutcome(
            "succeeded", "repaired", ("target.py",), (), True, True,
            "", "", (), False, datetime.now(UTC),
            baseline=GitWorkspaceState("main", "a" * 40),
            commit=CodingCommit("repair/target", "b" * 40, ("target.py",), True),
        )
        values = outcome.durable_values()

        self.assertEqual(values["branch"], "repair/target")
        self.assertEqual(values["commit_sha"], "b" * 40)
        self.assertEqual(values["commit"]["committed_files"], ["target.py"])
        self.assertTrue(values["commit"]["worktree_clean"])
        self.assertEqual(values["baseline"]["head_sha"], "a" * 40)

    def test_an_outcome_without_a_commit_states_no_branch(self) -> None:
        """Absent, not empty-stringed: Core must not read one that is not there."""
        from datetime import UTC, datetime

        from alx.contracts.coding import CodingOutcome

        outcome = CodingOutcome(
            "succeeded", "repaired", ("target.py",), (), True, True,
            "", "", (), False, datetime.now(UTC),
        )
        values = outcome.durable_values()
        self.assertNotIn("branch", values)
        self.assertNotIn("commit_sha", values)
        self.assertNotIn("commit", values)


if __name__ == "__main__":
    unittest.main()
