"""D-030: every coding job runs in an isolated worktree AL/X created.

The sandbox was never the gap. It restricts writes to whatever directory it is
told to restrict them to, and it did that correctly. What nothing guaranteed
was that the directory was isolated: Core handed in a path as a string, the
capability description named `"."` as valid, and `"."` is the live AL/X
checkout. A job pointed there had the whole repository made writable, exactly
as configured, which is the failure these tests exist to make impossible.

So the properties proved here are about where a job can end up, not about what
it may do once it is there:

- **the live checkout can never be it** — not by argument, because there is no
  argument; not by configuration, because a root resolving inside the
  repository refuses; and not by symlink, because resolution happens before the
  comparison;
- **one job, one worktree** — the same directory across every phase, reused by
  corrections, never shared with another job;
- **nothing is destroyed to make room** — a retained worktree from a failed job
  is yielded to rather than reused, and release removes only what this
  allocator itself created, for the job that owns it.

Every test runs against real git. Worktree linkage is a fact about a
repository, and a mock cannot be wrong about it in the way that matters.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import CodingError  # noqa: E402
from alx.providers.coding_git import git_write_permitted  # noqa: E402
from alx.providers.coding_worktree import (  # noqa: E402
    CodingWorktreeAllocator,
    job_id_permitted,
    resolve_worktree_root,
)


def git(repository: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv], cwd=repository, check=True,
        capture_output=True, text=True,
    )
    return completed.stdout


class Repository(unittest.TestCase):
    """A canonical repository, and a worktree root outside it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        enclosing = Path(self.directory.name).resolve()
        # The repository and the worktree root are siblings, so the root is
        # outside the repository without being somewhere unrelated on disk.
        self.repository = enclosing / "repo"
        self.repository.mkdir()
        self.root = enclosing / "coding-worktrees"
        git(self.repository, "init", "-q", "-b", "main")
        git(self.repository, "config", "user.email", "test@example.test")
        git(self.repository, "config", "user.name", "test")
        (self.repository / "target.py").write_text("original\n")
        git(self.repository, "add", "-A")
        git(self.repository, "commit", "-qm", "base")
        self.base_sha = git(self.repository, "rev-parse", "HEAD").strip()

    def allocator(self) -> CodingWorktreeAllocator:
        return CodingWorktreeAllocator(self.root, self.repository)

    def worktrees(self) -> str:
        return git(self.repository, "worktree", "list")


class TheLiveCheckoutIsNeverTheExecutionDirectory(Repository):
    """The one property the whole decision exists for."""

    def test_an_allocated_worktree_is_not_the_repository(self) -> None:
        allocated = self.allocator().allocate("job-1")
        self.assertNotEqual(allocated.path, self.repository)
        self.assertFalse(
            str(allocated.path).startswith(str(self.repository) + "/")
        )

    def test_a_root_inside_the_repository_refuses(self) -> None:
        inside = self.repository / ".alx" / "worktrees"
        with self.assertRaises(CodingError) as caught:
            CodingWorktreeAllocator(inside, self.repository)
        self.assertEqual(caught.exception.code, "worktree_unusable")
        self.assertEqual(
            caught.exception.details["reason_code"], "root_inside_repository"
        )

    def test_the_repository_itself_as_a_root_refuses(self) -> None:
        with self.assertRaises(CodingError) as caught:
            CodingWorktreeAllocator(self.repository, self.repository)
        self.assertEqual(
            caught.exception.details["reason_code"], "root_inside_repository"
        )

    def test_a_symlinked_root_pointing_into_the_repository_refuses(self) -> None:
        """Resolution happens before the comparison, so a link cannot hide it."""
        target = self.repository / "worktrees"
        target.mkdir()
        link = Path(self.directory.name).resolve() / "linked-root"
        link.symlink_to(target)

        with self.assertRaises(CodingError) as caught:
            CodingWorktreeAllocator(link, self.repository)

        self.assertEqual(
            caught.exception.details["reason_code"], "root_inside_repository"
        )

    def test_a_root_that_became_a_symlink_after_startup_refuses(self) -> None:
        """The invariant holds per allocation, not merely per process."""
        allocator = self.allocator()
        inside = self.repository / "sneaky"
        inside.mkdir()
        self.root.symlink_to(inside)

        with self.assertRaises(CodingError) as caught:
            allocator.allocate("job-1")

        self.assertEqual(
            caught.exception.details["reason_code"], "root_inside_repository"
        )

    def test_a_relative_root_is_resolved_not_assumed(self) -> None:
        resolved = resolve_worktree_root(self.root, self.repository)
        self.assertTrue(resolved.is_absolute())


class CoreSuppliesNoPath(Repository):
    """The capability accepts a job, never a directory."""

    def test_the_capability_schema_has_no_worktree_property(self) -> None:
        from alx.tools.coding import DEFINITION

        self.assertNotIn("worktree", DEFINITION.input_schema.properties)
        self.assertNotIn("job_id", DEFINITION.input_schema.properties)
        self.assertEqual(DEFINITION.input_schema.required, ("task",))

    def test_a_supplied_worktree_argument_is_refused_not_ignored(self) -> None:
        """Ignoring it would let a model believe it chose where a job runs."""
        from alx.tools.coding import parse_coding_arguments

        request, failure = parse_coding_arguments(
            {"task": "fix", "worktree": "."}, "call-1"
        )

        self.assertIsNone(request)
        self.assertEqual(failure["invalid_field"], "worktree")
        self.assertEqual(failure["reason_code"], "not_accepted")

    def test_a_supplied_job_id_is_refused(self) -> None:
        from alx.tools.coding import parse_coding_arguments

        request, failure = parse_coding_arguments(
            {"task": "fix", "job_id": "chosen"}, "call-1"
        )

        self.assertIsNone(request)
        self.assertEqual(failure["invalid_field"], "job_id")

    def test_the_broker_call_id_becomes_the_job_id(self) -> None:
        from alx.tools.coding import parse_coding_arguments

        request, failure = parse_coding_arguments({"task": "fix"}, "call-42")

        self.assertIsNone(failure)
        self.assertEqual(request.job_id, "call-42")

    def test_the_description_no_longer_offers_the_current_directory(self) -> None:
        from alx.tools.coding import DEFINITION

        self.assertNotIn('"."', DEFINITION.purpose)
        self.assertNotIn("worktree is a filesystem path", DEFINITION.purpose)


class AllocatingOneWorktreePerJob(Repository):
    """Branch and worktree arrive together, from the job's own identity."""

    def test_the_worktree_is_a_linked_worktree_of_the_repository(self) -> None:
        allocated = self.allocator().allocate("job-1")

        # A linked worktree's .git is a file pointing at the parent, not a
        # directory. This is what distinguishes it from a copy or a clone.
        pointer = allocated.path / ".git"
        self.assertTrue(pointer.is_file())
        self.assertIn(str(allocated.path), self.worktrees())

    def test_the_branch_and_the_worktree_are_created_together(self) -> None:
        allocated = self.allocator().allocate("job-1")

        self.assertEqual(
            git(allocated.path, "rev-parse", "--abbrev-ref", "HEAD").strip(),
            allocated.branch,
        )
        self.assertEqual(
            git(self.repository, "rev-parse", allocated.branch).strip(),
            self.base_sha,
        )

    def test_a_core_named_branch_is_kept(self) -> None:
        """Naming the repair is a judgement, so D-029's field still decides."""
        allocated = self.allocator().allocate("job-1", "repair/target")
        self.assertEqual(allocated.branch, "repair/target")

    def test_an_unnamed_branch_is_derived_mechanically(self) -> None:
        allocated = self.allocator().allocate("job-1")
        self.assertEqual(allocated.branch, "alx/coding/job-1")

    def test_the_worktree_is_named_for_the_job(self) -> None:
        allocated = self.allocator().allocate("job-1")
        self.assertEqual(allocated.path, self.root / "job-1")
        self.assertEqual(allocated.job_id, "job-1")

    def test_two_jobs_receive_distinct_worktrees_and_branches(self) -> None:
        first = self.allocator().allocate("job-1")
        second = self.allocator().allocate("job-2")

        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(first.branch, second.branch)
        self.assertTrue(first.path.is_dir())
        self.assertTrue(second.path.is_dir())

    def test_a_job_id_that_could_climb_out_of_the_root_is_refused(self) -> None:
        for job_id in ("../escape", "a/b", "-flag", "", "  "):
            self.assertFalse(job_id_permitted(job_id), job_id)
            with self.assertRaises(CodingError, msg=job_id):
                self.allocator().allocate(job_id)

    def test_the_new_worktree_starts_from_the_repository_head(self) -> None:
        allocated = self.allocator().allocate("job-1")
        self.assertEqual(allocated.base, self.base_sha)
        self.assertEqual(
            git(allocated.path, "rev-parse", "HEAD").strip(), self.base_sha
        )


class DirtInTheLiveCheckoutStaysThere(Repository):
    """A job starts from a committed baseline, not from somebody's edits."""

    def test_uncommitted_changes_do_not_reach_the_job(self) -> None:
        (self.repository / "target.py").write_text("somebody else was here\n")

        allocated = self.allocator().allocate("job-1")

        self.assertEqual(
            (allocated.path / "target.py").read_text(), "original\n"
        )

    def test_untracked_files_do_not_reach_the_job(self) -> None:
        (self.repository / "scratch.txt").write_text("notes\n")

        allocated = self.allocator().allocate("job-1")

        self.assertFalse((allocated.path / "scratch.txt").exists())

    def test_the_job_worktree_starts_clean(self) -> None:
        (self.repository / "target.py").write_text("dirty\n")
        (self.repository / "extra.txt").write_text("x\n")

        allocated = self.allocator().allocate("job-1")

        self.assertEqual(git(allocated.path, "status", "--porcelain"), "")


class CollisionHandlingIsUnchanged(Repository):
    """D-029's scheme, applied to the one command that now allocates both."""

    def test_an_occupied_branch_name_takes_the_second_suffix(self) -> None:
        git(self.repository, "branch", "alx/coding/job-1")
        original = git(self.repository, "rev-parse", "alx/coding/job-1").strip()

        allocated = self.allocator().allocate("job-1")

        self.assertEqual(allocated.branch, "alx/coding/job-1-2")
        self.assertEqual(allocated.path, self.root / "job-1-2")
        self.assertEqual(
            git(self.repository, "rev-parse", "alx/coding/job-1").strip(),
            original,
        )

    def test_multiple_collisions_select_the_first_available_suffix(self) -> None:
        for name in ("alx/coding/job-1", "alx/coding/job-1-2", "alx/coding/job-1-3"):
            git(self.repository, "branch", name)

        allocated = self.allocator().allocate("job-1")

        self.assertEqual(allocated.branch, "alx/coding/job-1-4")

    def test_a_retained_worktree_is_yielded_to_rather_than_reused(self) -> None:
        """A failed job's evidence is not overwritten by the next attempt."""
        first = self.allocator().allocate("job-1")
        (first.path / "partial.py").write_text("half-done\n")

        second = self.allocator().allocate("job-1")

        self.assertNotEqual(second.path, first.path)
        self.assertEqual(
            (first.path / "partial.py").read_text(), "half-done\n"
        )

    def test_occupied_branches_are_ref_for_ref_untouched(self) -> None:
        git(self.repository, "branch", "alx/coding/job-1")
        before = git(self.repository, "for-each-ref", "refs/heads/")

        self.allocator().allocate("job-1")

        after = git(self.repository, "for-each-ref", "refs/heads/")
        for line in before.splitlines():
            self.assertIn(line, after)

    def test_a_non_collision_failure_stops_rather_than_retrying(self) -> None:
        """A lock or permission failure must not be hidden by a suffix search."""
        from alx.providers import coding_git

        failure = coding_git._GitResult(
            128, "", "fatal: Unable to create '.git/index.lock': File exists\n"
        )
        real_run = coding_git._run

        def fail_add(root, argv, **kwargs):
            if argv[:3] == ["git", "worktree", "add"]:
                return failure
            return real_run(root, argv, **kwargs)

        with unittest.mock.patch.object(
            coding_git, "_run", side_effect=fail_add
        ) as run:
            with self.assertRaises(CodingError) as caught:
                self.allocator().allocate("job-1")

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"], "worktree_not_created"
        )
        attempts = [
            call for call in run.call_args_list
            if call.args[1][:3] == ["git", "worktree", "add"]
        ]
        self.assertEqual(len(attempts), 1)

    def test_suffix_exhaustion_fails_closed(self) -> None:
        from alx.providers import coding_worktree

        with unittest.mock.patch.object(
            coding_worktree, "MAX_REPAIR_BRANCH_ATTEMPTS", 2
        ):
            for name in ("alx/coding/job-1", "alx/coding/job-1-2"):
                git(self.repository, "branch", name)
            with self.assertRaises(CodingError) as caught:
                self.allocator().allocate("job-1")

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertEqual(
            caught.exception.details["reason_code"],
            "worktree_attempts_exhausted",
        )


class ReleasingAWorkspace(Repository):
    """Removal proves ownership first, and removes nothing when it cannot."""

    def test_a_job_worktree_this_allocator_created_is_removed(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")

        allocator.release(allocated.job_id, allocated.path)

        self.assertFalse(allocated.path.exists())
        self.assertNotIn(str(allocated.path), self.worktrees())

    def test_the_branch_and_its_commits_survive_release(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        (allocated.path / "target.py").write_text("repaired\n")
        git(allocated.path, "add", "-A")
        git(allocated.path, "-c", "user.email=t@e.test", "-c", "user.name=t",
            "commit", "-qm", "repair")
        repaired = git(allocated.path, "rev-parse", "HEAD").strip()

        allocator.release(allocated.job_id, allocated.path)

        self.assertEqual(
            git(self.repository, "rev-parse", allocated.branch).strip(), repaired
        )

    def test_release_cannot_remove_the_live_checkout(self) -> None:
        allocator = self.allocator()
        allocator.allocate("job-1")

        with self.assertRaises(CodingError) as caught:
            allocator.release("job-1", self.repository)

        self.assertEqual(caught.exception.code, "worktree_unusable")
        self.assertTrue(self.repository.is_dir())
        self.assertTrue((self.repository / "target.py").exists())

    def test_release_cannot_remove_an_arbitrary_path(self) -> None:
        allocator = self.allocator()
        allocator.allocate("job-1")
        elsewhere = Path(self.directory.name).resolve() / "unrelated"
        elsewhere.mkdir()
        (elsewhere / "keep.txt").write_text("keep\n")

        with self.assertRaises(CodingError) as caught:
            allocator.release("job-1", elsewhere)

        self.assertEqual(
            caught.exception.details["reason_code"], "worktree_not_owned_by_job"
        )
        self.assertTrue((elsewhere / "keep.txt").exists())

    def test_release_cannot_remove_another_jobs_worktree(self) -> None:
        allocator = self.allocator()
        first = allocator.allocate("job-1")
        second = allocator.allocate("job-2")

        with self.assertRaises(CodingError) as caught:
            allocator.release("job-1", second.path)

        self.assertEqual(
            caught.exception.details["reason_code"], "worktree_not_owned_by_job"
        )
        self.assertTrue(second.path.is_dir())
        self.assertTrue(first.path.is_dir())

    def test_release_refuses_a_directory_it_did_not_allocate(self) -> None:
        """Right name, right place, but no record and no worktree linkage.

        A directory this allocator never created has no allocation record, so
        the ownership question is refused before the directory is examined at
        all. That ordering is deliberate: the record is what ties a directory
        to a job, and without one there is nothing to check it against.
        """
        allocator = self.allocator()
        impostor = self.root / "job-9"
        impostor.mkdir(parents=True)
        (impostor / "keep.txt").write_text("keep\n")

        with self.assertRaises(CodingError) as caught:
            allocator.release("job-9", impostor)

        self.assertEqual(
            caught.exception.details["reason_code"],
            "worktree_not_owned_by_job",
        )
        self.assertTrue((impostor / "keep.txt").exists())

    def test_release_refuses_a_recorded_job_whose_directory_is_an_impostor(
        self,
    ) -> None:
        """A record is necessary but not sufficient: git still has to agree."""
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        # Replace the real worktree with an ordinary directory of the same
        # name, so the record still points at it but it is no longer linked.
        git(self.repository, "worktree", "remove", str(allocated.path))
        allocated.path.mkdir(parents=True)
        (allocated.path / "keep.txt").write_text("keep\n")

        with self.assertRaises(CodingError) as caught:
            allocator.release("job-1", allocated.path)

        self.assertEqual(
            caught.exception.details["reason_code"],
            "worktree_not_allocated_here",
        )
        self.assertTrue((allocated.path / "keep.txt").exists())

    def test_a_worktree_holding_uncommitted_work_is_not_discarded(self) -> None:
        """No --force: unaccounted work refuses release rather than vanishing."""
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        (allocated.path / "target.py").write_text("unfinished\n")

        with self.assertRaises(CodingError) as caught:
            allocator.release(allocated.job_id, allocated.path)

        self.assertEqual(caught.exception.code, "git_refused")
        self.assertTrue(allocated.path.is_dir())
        self.assertEqual(
            (allocated.path / "target.py").read_text(), "unfinished\n"
        )

    def test_ownership_is_structural_not_remembered(self) -> None:
        """A fresh allocator still recognises a worktree after a restart."""
        allocated = self.allocator().allocate("job-1")

        restarted = self.allocator()

        self.assertTrue(restarted.owns(allocated.path))
        restarted.release(allocated.job_id, allocated.path)
        self.assertFalse(allocated.path.exists())

    def test_a_directory_outside_the_root_is_not_owned(self) -> None:
        allocator = self.allocator()
        self.assertFalse(allocator.owns(self.repository))
        self.assertFalse(allocator.owns(self.root / "nested" / "deep"))


class ReleaseRequiresAnExplicitCoreDecision(Repository):
    """D-030: success is not release. Core has to ask, and it is recorded."""

    def _runtime(self, allocator):
        from alx.bootstrap.coding import build_coding_runtime
        from test_coding_agent import PlanningModel, RecordingSession

        return build_coding_runtime(
            True, PlanningModel(), lambda: "call-1",
            session=RecordingSession(), reviewer=PlanningModel(),
            allocator=allocator,
        )

    def _release(self, allocator, job_id: str):
        runtime = self._runtime(allocator)
        return runtime.executors["release_coding_workspace"]({"job_id": job_id})

    def test_a_succeeded_job_is_released_when_core_asks(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")

        result = self._release(allocator, "job-1")

        self.assertEqual(result.state.value, "succeeded")
        self.assertTrue(result.values["released"])
        self.assertFalse(allocated.path.exists())

    def test_a_succeeded_job_is_retained_until_core_asks(self) -> None:
        """The whole point: finishing is not the trigger."""
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")

        self.assertTrue(allocated.path.is_dir())
        self.assertIn("job-1", allocator.stale_job_ids())

    def test_a_failed_job_cannot_be_released(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "failed")

        result = self._release(allocator, "job-1")

        self.assertEqual(result.state.value, "failed")
        self.assertEqual(result.failure["code"], "job_not_successful")
        self.assertTrue(allocated.path.is_dir())

    def test_an_unfinished_job_cannot_be_released(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")

        result = self._release(allocator, "job-1")

        self.assertEqual(result.failure["code"], "job_not_successful")
        self.assertEqual(result.failure["status"], "unfinished")
        self.assertTrue(allocated.path.is_dir())

    def test_an_unknown_job_releases_nothing(self) -> None:
        allocator = self.allocator()
        allocator.allocate("job-1")

        result = self._release(allocator, "job-2")

        self.assertEqual(result.state.value, "failed")
        self.assertEqual(result.values or {}, {})

    def test_the_release_capability_accepts_no_path(self) -> None:
        from alx.tools.coding import RELEASE_DEFINITION

        self.assertEqual(
            tuple(RELEASE_DEFINITION.input_schema.properties), ("job_id",)
        )
        self.assertEqual(RELEASE_DEFINITION.input_schema.required, ("job_id",))

    def test_the_release_is_durable_and_auditable(self) -> None:
        """The invocation record is the authorisation D-030 requires."""
        from alx.tools.coding import RELEASE_DEFINITION

        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")

        result = self._release(allocator, "job-1")

        self.assertEqual(RELEASE_DEFINITION.durable_input_fields, ("job_id",))
        self.assertEqual(result.durable_values["job_id"], "job-1")
        self.assertEqual(
            result.durable_values["worktree"], str(allocated.path)
        )
        self.assertEqual(result.durable_values["branch"], allocated.branch)
        self.assertTrue(result.durable_values["released"])

    def test_release_is_a_separate_capability_from_running_a_job(self) -> None:
        """Core must choose it, rather than it following from a job."""
        runtime = self._runtime(self.allocator())
        self.assertEqual(
            sorted(runtime.executors),
            ["release_coding_workspace", "run_coding_task"],
        )


class TheGitAuthorityThisNeeds(unittest.TestCase):
    """D-030 grants two worktree shapes and nothing adjacent to them."""

    def test_the_two_granted_shapes_are_permitted(self) -> None:
        self.assertTrue(git_write_permitted(
            ["git", "worktree", "add", "-b", "fix/x", "/tmp/w", "abc123"]
        ))
        self.assertTrue(git_write_permitted(["git", "worktree", "remove", "/tmp/w"]))

    def test_pruning_is_not_authorised(self) -> None:
        self.assertFalse(git_write_permitted(["git", "worktree", "prune"]))

    def test_forced_removal_is_not_authorised(self) -> None:
        self.assertFalse(git_write_permitted(
            ["git", "worktree", "remove", "--force", "/tmp/w"]
        ))

    def test_other_worktree_subcommands_are_not_authorised(self) -> None:
        for argv in (
            ["git", "worktree", "move", "/tmp/a", "/tmp/b"],
            ["git", "worktree", "lock", "/tmp/w"],
            ["git", "worktree", "unlock", "/tmp/w"],
            ["git", "worktree", "repair"],
            ["git", "worktree", "list"],
        ):
            self.assertFalse(git_write_permitted(argv), " ".join(argv))

    def test_adding_without_a_new_branch_is_not_authorised(self) -> None:
        """Detaching onto an existing ref is not a shape that can be built."""
        self.assertFalse(git_write_permitted(
            ["git", "worktree", "add", "/tmp/w", "main"]
        ))
        self.assertFalse(git_write_permitted(
            ["git", "worktree", "add", "--detach", "/tmp/w"]
        ))


if __name__ == "__main__":
    unittest.main()
