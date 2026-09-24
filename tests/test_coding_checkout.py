"""D-033's single-checkout branch preparation and exclusivity boundary."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import CodingError  # noqa: E402
from alx.providers.coding_git import (  # noqa: E402
    coding_job_lock,
    prepare_feature_branch,
)
from alx.providers.coding_workspace import CodingWorkspace  # noqa: E402


def git(repository: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=repository, check=True, capture_output=True, text=True
    ).stdout


class CanonicalCheckout(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.test")
        git(self.root, "config", "user.name", "test")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        git(self.root, "add", "app.py")
        git(self.root, "commit", "-qm", "base")
        self.base = git(self.root, "rev-parse", "HEAD").strip()

    def test_feature_branch_is_created_in_the_visible_checkout(self) -> None:
        branch = prepare_feature_branch(self.root, "feat/change")

        self.assertEqual(branch, "feat/change")
        self.assertEqual(
            git(self.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), branch
        )
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), self.base)
        self.assertTrue((self.root / ".git").is_dir())

    def test_existing_branch_gets_a_bounded_suffix(self) -> None:
        git(self.root, "branch", "feat/change")
        self.assertEqual(prepare_feature_branch(self.root, "feat/change"), "feat/change-2")

    def test_dirty_main_is_refused_before_branch_creation(self) -> None:
        (self.root / "notes.txt").write_text("mine\n", encoding="utf-8")
        with self.assertRaises(CodingError) as caught:
            prepare_feature_branch(self.root, "feat/change")
        self.assertEqual(caught.exception.details["reason_code"], "canonical_checkout_dirty")
        self.assertEqual(
            git(self.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main"
        )

    def test_non_main_checkout_is_refused(self) -> None:
        git(self.root, "switch", "-q", "-c", "other")
        with self.assertRaises(CodingError) as caught:
            prepare_feature_branch(self.root, "feat/change")
        self.assertEqual(
            caught.exception.details["reason_code"], "canonical_checkout_not_on_main"
        )

    def test_main_cannot_be_the_implementation_branch(self) -> None:
        with self.assertRaises(CodingError) as caught:
            prepare_feature_branch(self.root, "main")
        self.assertEqual(caught.exception.details["reason_code"], "branch_name_not_permitted")

    def test_second_implementation_lock_is_refused_immediately(self) -> None:
        with coding_job_lock(self.root):
            with self.assertRaises(CodingError) as caught:
                with coding_job_lock(self.root):
                    self.fail("the second implementation job acquired the lock")
        self.assertEqual(caught.exception.details["reason_code"], "coding_job_active")

    def test_linked_worktree_is_not_accepted_as_the_canonical_checkout(self) -> None:
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        linked = Path(parent.name).resolve() / "linked"
        git(self.root, "worktree", "add", "-q", "-b", "linked-test", str(linked))
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(linked)],
                cwd=self.root, capture_output=True, text=True,
            )
        )
        with self.assertRaises(CodingError) as preflight:
            prepare_feature_branch(linked, "feat/change")
        self.assertEqual(
            preflight.exception.details["reason_code"], "not_canonical_checkout"
        )
        self.assertEqual(
            git(linked, "rev-parse", "--abbrev-ref", "HEAD").strip(),
            "linked-test",
        )
        with self.assertRaises(CodingError) as caught:
            CodingWorkspace(str(linked))
        self.assertEqual(caught.exception.details["reason_code"], "not_canonical_checkout")


class SupersededPathIsGone(unittest.TestCase):
    def test_worktree_allocator_and_release_capability_cannot_return(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertFalse(
            (repository / "src/alx/providers/coding_worktree.py").exists()
        )
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (repository / "src/alx").rglob("*.py")
        )
        self.assertNotIn("release_coding_workspace", production)
        self.assertNotIn("CodingWorktreeAllocator", production)


if __name__ == "__main__":
    unittest.main()
