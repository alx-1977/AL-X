"""A coding job's diff evidence is its own files, and says when it is clipped.

On 2026-09-11 four coding sessions on one goal were handed the same diff
digest, dc1788a..., across three hours and three different file sets. The
worktree they ran in carried 109k of somebody else's uncommitted work, and
`git diff` emits alphabetically: the 32k bound cut before any file the job had
been asked to repair. Two sessions timed out searching for context they were
never given; two completed having written nothing.

Two defects, both fixed here:

- The diff covered the whole worktree. A job does not own the tree it runs in,
  so unrelated dirt spent the budget. The diff is now narrowed to the files
  the job actually touched, using the file set the agent already computes.
  Status still reports the whole tree, because what else is dirty is a fact
  the job needs.

- Bounding was invisible. A clipped diff and a complete one were the same
  string, so nothing downstream could tell them apart. The length before
  bounding is now carried with the evidence, and a clipped diff is labelled.

Neither bound is raised, and unrelated changes are left untouched on disk;
they are simply not this job's evidence.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import MAX_DIFF_CHARACTERS  # noqa: E402
from alx.providers.coding_process import (  # noqa: E402
    command_permitted,
    files_from_git_status,
    inspect_git,
)


def git(repository: Path, *argv: str) -> None:
    subprocess.run(
        ["git", *argv], cwd=repository, check=True,
        capture_output=True, text=True,
    )


class Repository(unittest.TestCase):
    """A real git worktree, because the evidence comes from real git."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.test")
        git(self.root, "config", "user.name", "test")
        (self.root / "target.py").write_text("original\n")
        (self.root / "unrelated.py").write_text("original\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")

    def dirty(self, name: str, characters: int) -> None:
        (self.root / name).write_text("x" * characters + "\n")


class UnrelatedWorkDoesNotSpendTheBudget(Repository):
    """The live defect: somebody else's dirt crowding out the job's files."""

    def test_a_scoped_diff_excludes_unrelated_dirty_files(self) -> None:
        self.dirty("unrelated.py", 200)
        self.dirty("target.py", 200)

        whole = inspect_git(self.root)
        scoped = inspect_git(self.root, ("target.py",))

        self.assertIn("unrelated.py", whole.diff)
        self.assertIn("target.py", whole.diff)
        # The job's evidence carries its file and not the other.
        self.assertIn("target.py", scoped.diff)
        self.assertNotIn("unrelated.py", scoped.diff)

    def test_the_target_appears_even_when_the_tree_diff_exceeds_the_bound(self) -> None:
        """The exact live shape: target sorts last, unrelated dirt is huge."""
        self.dirty("unrelated.py", MAX_DIFF_CHARACTERS * 2)
        self.dirty("target.py", 200)

        whole = inspect_git(self.root)
        scoped = inspect_git(self.root, ("target.py",))

        # The whole-tree diff is over the bound and says so. Which files
        # survive the cut depends on alphabetical order, which is exactly the
        # accident that hid the DHL files; the point is that the job no longer
        # depends on winning that lottery.
        self.assertTrue(whole.diff_truncated)
        self.assertGreater(whole.diff_characters, MAX_DIFF_CHARACTERS)
        # Whatever the ordering, the scoped diff is complete and is the job's.
        self.assertFalse(scoped.diff_truncated)
        self.assertIn("target.py", scoped.diff)
        self.assertNotIn("unrelated.py", scoped.diff)

    def test_unrelated_files_are_not_altered_on_disk(self) -> None:
        """Scoping is about evidence, never about cleaning the tree."""
        self.dirty("unrelated.py", 200)
        inspect_git(self.root, ("target.py",))
        self.assertEqual(
            (self.root / "unrelated.py").read_text(), "x" * 200 + "\n"
        )
        # And status still reports it, because that is a fact the job needs.
        self.assertIn("unrelated.py", inspect_git(self.root, ("target.py",)).status)

    def test_literal_spaces_and_quotes_remain_valid_scoped_paths(self) -> None:
        name = 'space "quoted" name.py'
        (self.root / name).write_text("original\n")
        git(self.root, "add", name)
        git(self.root, "commit", "-qm", "add quoted target")
        self.dirty(name, 200)
        evidence = inspect_git(self.root, (name,))

        self.assertIn(name, files_from_git_status(evidence.status))
        self.assertTrue(evidence.diff)
        self.assertIn("quoted", evidence.diff)
        self.assertFalse(evidence.diff_truncated)


class TruncationIsStated(Repository):
    """A clipped diff may not be presented as a complete one."""

    def test_a_small_diff_reports_itself_complete(self) -> None:
        self.dirty("target.py", 100)
        evidence = inspect_git(self.root, ("target.py",))
        self.assertFalse(evidence.diff_truncated)
        self.assertEqual(evidence.diff_characters, len(evidence.diff))

    def test_a_scoped_diff_over_the_bound_is_flagged(self) -> None:
        """Narrowing helps; it does not guarantee the diff fits."""
        self.dirty("target.py", MAX_DIFF_CHARACTERS * 2)
        evidence = inspect_git(self.root, ("target.py",))
        self.assertTrue(evidence.diff_truncated)
        self.assertEqual(len(evidence.diff), MAX_DIFF_CHARACTERS)
        self.assertGreater(evidence.diff_characters, MAX_DIFF_CHARACTERS)

    def test_the_length_reported_is_before_bounding(self) -> None:
        """It used to describe an already-shortened string, twice over."""
        self.dirty("target.py", MAX_DIFF_CHARACTERS * 3)
        evidence = inspect_git(self.root, ("target.py",))
        self.assertGreater(evidence.diff_characters, MAX_DIFF_CHARACTERS * 2)

    def test_the_bound_itself_is_unchanged(self) -> None:
        self.assertEqual(MAX_DIFF_CHARACTERS, 32000)


class ScopedDiffStaysInsideTheWorktree(unittest.TestCase):
    """Narrowing must not become a way to read somewhere else."""

    def test_a_pathspec_inside_the_worktree_is_permitted(self) -> None:
        root = Path(".").resolve()
        self.assertTrue(
            command_permitted(["git", "diff", "--", "src/alx/tools/dhl.py"], root)
        )

    def test_an_escaping_pathspec_is_refused(self) -> None:
        root = Path(".").resolve()
        for path in ("../outside.py", "/etc/passwd"):
            self.assertFalse(
                command_permitted(["git", "diff", "--", path], root),
                f"{path} must not be readable through a pathspec",
            )

    def test_a_blocked_pathspec_is_refused(self) -> None:
        root = Path(".").resolve()
        self.assertFalse(
            command_permitted(["git", "diff", "--", ".env"], root, (".env",))
        )

    def test_an_empty_pathspec_is_refused(self) -> None:
        self.assertFalse(
            command_permitted(["git", "diff", "--"], Path(".").resolve())
        )

    def test_the_unscoped_diff_is_still_permitted(self) -> None:
        self.assertTrue(command_permitted(["git", "diff"], Path(".").resolve()))


# The agent-side wiring -- which file set each call site passes, and the label
# the agent puts on a clipped diff -- is tested in
# tests/test_coding_diff_scope_wiring.py. Those call sites live inside the
# local-reviewer code, which is not in HEAD yet, so the tests travel with it
# rather than with this mechanism.


if __name__ == "__main__":
    unittest.main()
