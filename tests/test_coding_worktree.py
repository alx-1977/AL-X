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

import json
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

    def allocator(self, outcomes: dict[str, str] | None = None):
        """An allocator whose durable outcomes live outside the worktree root.

        `self.outcomes` stands in for the broker's own record of what each
        capability call returned. It is deliberately *not* a file under the
        allocator's root: the point of D-030's terminal-success check is that
        editing anything in that directory cannot make a failed job releasable.
        """
        if outcomes is not None:
            self.outcomes = outcomes
        elif not hasattr(self, "outcomes"):
            self.outcomes = {}
        return CodingWorktreeAllocator(
            self.root, self.repository, lambda job_id: self.outcomes.get(job_id, "")
        )

    def finished(self, job_id: str, status: str = "succeeded") -> None:
        """Record what the durable capability outcome says about a job."""
        if not hasattr(self, "outcomes"):
            self.outcomes = {}
        self.outcomes[job_id] = status

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
        self.finished("job-1", "succeeded")

        result = self._release(allocator, "job-1")

        self.assertEqual(result.state.value, "succeeded")
        self.assertTrue(result.values["released"])
        self.assertFalse(allocated.path.exists())

    def test_a_succeeded_job_is_retained_until_core_asks(self) -> None:
        """The whole point: finishing is not the trigger."""
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")
        self.finished("job-1", "succeeded")

        self.assertTrue(allocated.path.is_dir())
        self.assertIn("job-1", allocator.stale_job_ids())

    def test_a_failed_job_cannot_be_released(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "failed")
        self.finished("job-1", "failed")

        result = self._release(allocator, "job-1")

        self.assertEqual(result.state.value, "failed")
        self.assertEqual(result.failure["code"], "job_not_successful")
        self.assertTrue(allocated.path.is_dir())

    def test_an_unfinished_job_cannot_be_released(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")

        result = self._release(allocator, "job-1")

        # No durable outcome exists for a job that never finished, so there is
        # nothing to prove success with and the release refuses.
        self.assertEqual(result.failure["code"], "job_not_successful")
        self.assertEqual(
            result.failure["reason_code"], "durable_outcome_missing"
        )
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
        self.finished("job-1", "succeeded")

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


class ATamperedRecordCannotRedirectRelease(Repository):
    """The allocation record is a claim to be checked, never authority.

    It is an ordinary JSON file in an ordinary directory, so anything that can
    write there can edit it. Before this was closed, `release` resolved the
    worktree by reading `slot` straight out of the record: editing job A's
    record to name job B's slot and worktree made
    `release_coding_workspace("job-a")` remove **job B's** worktree, which is
    both the wrong directory and one whose own job never authorised anything.

    Every identity fact is now re-derived or read from git, and the record is
    compared against that: the slot must be one job A's own deterministic
    sequence could produce, the branch must be the branch git reports for the
    directory and must carry the same collision suffix as the slot, and the
    base must be a commit the canonical repository actually has.
    """

    def _record_path(self, allocator, job_id: str) -> Path:
        return allocator.root / f"{job_id}.allocation.json"

    def _tamper(self, allocator, whose: str, **fields) -> None:
        path = self._record_path(allocator, whose)
        record = json.loads(path.read_text())
        record.update(fields)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    def _two_jobs(self):
        allocator = self.allocator()
        first = allocator.allocate("job-a")
        second = allocator.allocate("job-b")
        allocator.record_outcome("job-a", "succeeded")
        self.finished("job-a", "succeeded")
        allocator.record_outcome("job-b", "succeeded")
        self.finished("job-b", "succeeded")
        return allocator, first, second

    def test_a_record_pointing_at_another_job_releases_nothing(self) -> None:
        """The exact reported defect."""
        allocator, first, second = self._two_jobs()
        self._tamper(
            allocator, "job-a",
            slot=second.slot, worktree=str(second.path),
        )

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-a")

        self.assertEqual(caught.exception.code, "worktree_unusable")
        # Neither worktree was touched.
        self.assertTrue(second.path.is_dir())
        self.assertTrue(first.path.is_dir())
        self.assertIn(str(second.path), self.worktrees())
        self.assertIn(str(first.path), self.worktrees())

    def test_matching_the_branch_too_still_releases_nothing(self) -> None:
        """Tampering consistently is still tampering."""
        allocator, first, second = self._two_jobs()
        self._tamper(
            allocator, "job-a",
            slot=second.slot, worktree=str(second.path), branch=second.branch,
        )

        with self.assertRaises(CodingError):
            allocator.release_authorised("job-a")

        self.assertTrue(second.path.is_dir())
        self.assertTrue(first.path.is_dir())

    def test_a_record_naming_another_jobs_worktree_alone_refuses(self) -> None:
        allocator, first, second = self._two_jobs()
        self._tamper(allocator, "job-a", worktree=str(second.path))

        with self.assertRaises(CodingError):
            allocator.release_authorised("job-a")

        self.assertTrue(second.path.is_dir())
        self.assertTrue(first.path.is_dir())

    def test_a_slot_outside_the_jobs_own_sequence_refuses(self) -> None:
        """`job-a` can only ever be `job-a`, `job-a-2`, `job-a-3`, ..."""
        allocator, first, _second = self._two_jobs()
        self._tamper(allocator, "job-a", slot="job-elsewhere")

        with self.assertRaises(CodingError):
            allocator.release_authorised("job-a")

        self.assertTrue(first.path.is_dir())

    def test_a_branch_the_worktree_is_not_on_refuses(self) -> None:
        allocator, first, _second = self._two_jobs()
        self._tamper(allocator, "job-a", branch="alx/coding/something-else")

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-a")

        self.assertEqual(caught.exception.details["detail"], "branch")
        self.assertTrue(first.path.is_dir())

    def test_a_base_the_repository_does_not_have_refuses(self) -> None:
        allocator, first, _second = self._two_jobs()
        self._tamper(allocator, "job-a", base="0" * 40)

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-a")

        self.assertEqual(caught.exception.details["detail"], "base")
        self.assertTrue(first.path.is_dir())

    def test_a_forged_job_id_refuses(self) -> None:
        allocator, first, _second = self._two_jobs()
        self._tamper(allocator, "job-a", job_id="job-b")

        with self.assertRaises(CodingError):
            allocator.release_authorised("job-a")

        self.assertTrue(first.path.is_dir())

    def test_an_untampered_record_still_releases(self) -> None:
        """The checks refuse tampering, not ordinary use."""
        allocator, first, second = self._two_jobs()

        released = allocator.release_authorised("job-a")

        self.assertTrue(released["released"])
        self.assertFalse(first.path.exists())
        # The other job is unaffected by its neighbour's release.
        self.assertTrue(second.path.is_dir())

    def test_a_collision_suffixed_job_still_releases(self) -> None:
        """Re-derivation must not break the slot a collision actually gave."""
        git(self.repository, "branch", "alx/coding/job-c")
        allocator = self.allocator()
        allocated = allocator.allocate("job-c")
        allocator.record_outcome("job-c", "succeeded")
        self.finished("job-c", "succeeded")
        self.assertEqual(allocated.slot, "job-c-2")

        released = allocator.release_authorised("job-c")

        self.assertTrue(released["released"])
        self.assertFalse(allocated.path.exists())


class OverlappingJobsShareNothing(Repository):
    """Two concurrent runs on one agent must not see each other's job.

    One runtime builds one `CodingAgent` and dispatches every coding job
    through it, so instance attributes are shared by every job that agent ever
    runs. `_allocated`, `_telemetry` and `_current_activity` were exactly that:
    the second job to start overwrote all three, after which the first could
    resolve the second's worktree, report under its identity and elapsed time,
    and write its terminal outcome onto the second's allocation record.
    """

    def setUp(self) -> None:
        super().setUp()
        # These jobs run to a real terminal outcome, so the repository needs
        # the module the session edits and a test AL/X's verification can run.
        (self.repository / "app.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8"
        )
        (self.repository / "test_app.py").write_text(
            "import unittest\nfrom app import add\n\n\n"
            "class AddTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(1, 2), 3)\n",
            encoding="utf-8",
        )
        git(self.repository, "add", "-A")
        git(self.repository, "commit", "-qm", "app under test")

    def _agent(self, allocator, sink=None, barrier=None):
        from test_coding_agent import PlanningModel, RecordingSession, _FIXED
        from alx.providers import coding_agent as module

        class Overlapping(RecordingSession):
            """Holds inside the session so the two runs genuinely overlap."""

            def run_session(self, request, briefing):
                if barrier is not None:
                    barrier.wait(timeout=10)
                return super().run_session(request, briefing)

        class ConcurrentReviewer(PlanningModel):
            """A reviewer stub two overlapping jobs can share.

            `PlanningModel` scripts its review answers in a list it pops from,
            which suits one job and empties under two. That is the stub's
            limitation, not the agent's: the agent under test is the production
            one, and what these tests prove is that two runs through it stay
            bound to their own job. Reviews are re-answered rather than
            consumed so the stub is not the thing that fails.
            """

            def complete(self, request):
                if request.output_schema_name == "alx_coding_local_review":
                    from alx.contracts import ModelCompletion

                    self.requests.append(request)
                    return ModelCompletion("xai", "scripted", {"findings": []})
                return super().complete(request)

        return module.CodingAgent(
            ConcurrentReviewer(), Overlapping(edits={"app.py": _FIXED}),
            ConcurrentReviewer(), telemetry_sink=sink, allocator=allocator,
        )

    def test_two_overlapping_runs_keep_their_own_worktrees(self) -> None:
        import threading

        from alx.contracts.coding import CodingRequest

        allocator = self.allocator()
        barrier = threading.Barrier(2)
        telemetry: list = []
        lock = threading.Lock()

        def sink(item):
            with lock:
                telemetry.append(item)

        agent = self._agent(allocator, sink=sink, barrier=barrier)
        outcomes: dict[str, object] = {}

        def run(job_id: str) -> None:
            outcomes[job_id] = agent.run(
                CodingRequest(task="fix add", job_id=job_id)
            )

        threads = [
            threading.Thread(target=run, args=(job_id,))
            for job_id in ("job-one", "job-two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        first = outcomes["job-one"]
        second = outcomes["job-two"]
        # Each outcome reports its own job and its own directory.
        self.assertEqual(first.job_id, "job-one")
        self.assertEqual(second.job_id, "job-two")
        self.assertNotEqual(first.worktree, second.worktree)
        self.assertTrue(first.worktree.endswith("job-one"))
        self.assertTrue(second.worktree.endswith("job-two"))
        # And each worktree really is its own, on its own branch.
        for outcome, job_id in ((first, "job-one"), (second, "job-two")):
            self.assertEqual(
                git(Path(outcome.worktree), "rev-parse", "--abbrev-ref", "HEAD").strip(),
                f"alx/coding/{job_id}",
            )

    def test_each_job_writes_its_own_terminal_bookkeeping(self) -> None:
        import threading

        from alx.contracts.coding import CodingRequest

        allocator = self.allocator()
        barrier = threading.Barrier(2)
        agent = self._agent(allocator, barrier=barrier)

        threads = [
            threading.Thread(
                target=agent.run,
                args=(CodingRequest(task="fix add", job_id=job_id),),
            )
            for job_id in ("job-one", "job-two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        # Each job's own record carries its own outcome, slot and branch.
        for job_id in ("job-one", "job-two"):
            record = allocator.read_record(job_id)
            self.assertIsNotNone(record, job_id)
            self.assertEqual(record["job_id"], job_id)
            self.assertEqual(record["slot"], job_id)
            self.assertEqual(record["branch"], f"alx/coding/{job_id}")
            self.assertEqual(record["status"], "succeeded")
            self.assertTrue(record["worktree"].endswith(job_id))

    def test_telemetry_is_attributed_per_job(self) -> None:
        import threading

        from alx.contracts.coding import CodingRequest

        allocator = self.allocator()
        barrier = threading.Barrier(2)
        telemetry: list = []
        lock = threading.Lock()

        def sink(item):
            with lock:
                telemetry.append(item)

        agent = self._agent(allocator, sink=sink, barrier=barrier)
        threads = [
            threading.Thread(
                target=agent.run,
                args=(CodingRequest(task="fix add", job_id=job_id),),
            )
            for job_id in ("job-one", "job-two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        reported = {item.job_id for item in telemetry}
        self.assertEqual(reported, {"job-one", "job-two"})
        # Each job reaches its own terminal observation exactly once.
        for job_id in ("job-one", "job-two"):
            terminal = [
                item for item in telemetry
                if item.job_id == job_id and item.terminal
            ]
            self.assertEqual(len(terminal), 1, job_id)
            self.assertEqual(terminal[0].outcome, "succeeded")

    def test_the_agent_holds_no_per_job_attributes(self) -> None:
        """A structural check, so the shape cannot quietly regress."""
        allocator = self.allocator()
        agent = self._agent(allocator)
        for name in ("_allocated", "_telemetry", "_current_activity"):
            self.assertFalse(
                hasattr(agent, name),
                f"{name} is per-job state and must not live on the agent",
            )


class AnUnrecordedWorktreeIsAuditable(Repository):
    """`git worktree add` succeeded and the record write did not.

    The worktree exists, on a real branch, and nothing names it. D-030 does not
    authorise deleting it — removal requires an explicit Core release, and
    there is now no record to prove this one is releasable — so the smallest
    design consistent with the decision keeps it and makes it *findable*: an
    orphan marker explains it, and `orphan_worktrees` discovers it from git
    even if that marker could not be written either.
    """

    def _fail_record_write(self, allocator):
        from alx.providers import coding_worktree as module

        def refuse(self, allocated):
            raise OSError("no space left on device")

        return unittest.mock.patch.object(
            module.CodingWorktreeAllocator, "_write_record", refuse
        )

    def test_allocation_fails_closed_when_the_record_cannot_be_written(
        self,
    ) -> None:
        allocator = self.allocator()

        with self._fail_record_write(allocator):
            with self.assertRaises(CodingError) as caught:
                allocator.allocate("job-1")

        self.assertEqual(caught.exception.code, "worktree_unusable")
        self.assertEqual(
            caught.exception.details["reason_code"],
            "allocation_record_not_written",
        )
        # The failure names the directory it could not account for.
        self.assertTrue(caught.exception.details["worktree"].endswith("job-1"))

    def test_the_worktree_is_kept_rather_than_silently_deleted(self) -> None:
        allocator = self.allocator()

        with self._fail_record_write(allocator):
            with self.assertRaises(CodingError):
                allocator.allocate("job-1")

        path = allocator.root / "job-1"
        self.assertTrue(path.is_dir())
        self.assertIn(str(path), self.worktrees())
        # And its branch survives with it.
        self.assertEqual(
            git(self.repository, "rev-parse", "--abbrev-ref", "alx/coding/job-1").strip(),
            "alx/coding/job-1",
        )

    def test_the_orphan_is_discoverable_and_explained(self) -> None:
        allocator = self.allocator()

        with self._fail_record_write(allocator):
            with self.assertRaises(CodingError):
                allocator.allocate("job-1")

        orphans = allocator.orphan_worktrees()

        self.assertEqual(len(orphans), 1)
        entry = orphans[0]
        self.assertEqual(entry["slot"], "job-1")
        self.assertEqual(entry["branch"], "alx/coding/job-1")
        self.assertTrue(entry["worktree"].endswith("job-1"))
        self.assertEqual(entry["noted"]["job_id"], "job-1")
        self.assertIn("could not be written", entry["noted"]["reason"])

    def test_an_orphan_is_still_found_without_its_marker(self) -> None:
        """Discovery asks git, so it does not depend on the marker landing."""
        allocator = self.allocator()

        with self._fail_record_write(allocator):
            with self.assertRaises(CodingError):
                allocator.allocate("job-1")
        (allocator.root / "job-1.orphan.json").unlink()

        orphans = allocator.orphan_worktrees()

        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["slot"], "job-1")
        self.assertNotIn("noted", orphans[0])

    def test_an_orphan_cannot_be_released(self) -> None:
        """A marker explains the orphan; it must not authorise removing it."""
        allocator = self.allocator()

        with self._fail_record_write(allocator):
            with self.assertRaises(CodingError):
                allocator.allocate("job-1")

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-1")

        self.assertEqual(
            caught.exception.details["reason_code"], "allocation_record_missing"
        )
        self.assertTrue((allocator.root / "job-1").is_dir())

    def test_a_properly_recorded_worktree_is_not_an_orphan(self) -> None:
        allocator = self.allocator()
        allocator.allocate("job-1")

        self.assertEqual(allocator.orphan_worktrees(), ())

    def test_a_later_job_does_not_reuse_the_orphans_slot(self) -> None:
        """The orphan is retained, so the next job yields to it."""
        allocator = self.allocator()
        with self._fail_record_write(allocator):
            with self.assertRaises(CodingError):
                allocator.allocate("job-1")

        allocated = allocator.allocate("job-1")

        self.assertEqual(allocated.slot, "job-1-2")
        self.assertTrue((allocator.root / "job-1").is_dir())
        self.assertEqual(len(allocator.orphan_worktrees()), 1)


class TerminalSuccessComesFromTheDurableOutcome(Repository):
    """Editing the sidecar's status must not make a failed job releasable.

    The allocation record's `status` is audit metadata written beside the
    workspace. It used to be what `release_authorised` consulted, so changing
    `failed` to `succeeded` in that file released a failed job's worktree —
    destroying exactly the evidence D-030 retains it for.

    Terminal success is now established from the durable capability outcome the
    broker recorded when the job returned, which lives in the goal store rather
    than in the coding-worktree root. The sidecar stays descriptive.
    """

    def test_tampering_failed_to_succeeded_releases_nothing(self) -> None:
        """The exact reported defect."""
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "failed")
        self.finished("job-1", "failed")

        path = allocator.root / "job-1.allocation.json"
        record = json.loads(path.read_text())
        record["status"] = "succeeded"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-1")

        self.assertEqual(caught.exception.code, "job_not_successful")
        self.assertEqual(
            caught.exception.details["reason_code"], "job_did_not_succeed"
        )
        self.assertEqual(caught.exception.details["status"], "failed")
        # The failed job's evidence is still there.
        self.assertTrue(allocated.path.is_dir())
        self.assertIn(str(allocated.path), self.worktrees())

    def test_a_sidecar_disagreeing_with_a_success_also_refuses(self) -> None:
        """Disagreement is refused rather than resolved in either direction."""
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "failed")
        self.finished("job-1", "succeeded")

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-1")

        self.assertEqual(
            caught.exception.details["reason_code"], "outcome_record_conflicts"
        )
        self.assertTrue(allocated.path.is_dir())

    def test_no_durable_outcome_refuses(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")
        # Nothing recorded in the durable store for this job.

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-1")

        self.assertEqual(
            caught.exception.details["reason_code"], "durable_outcome_missing"
        )
        self.assertTrue(allocated.path.is_dir())

    def test_no_outcome_source_at_all_refuses(self) -> None:
        """Fail closed: an allocator that cannot ask does not release."""
        from alx.providers.coding_worktree import CodingWorktreeAllocator

        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")
        unwired = CodingWorktreeAllocator(self.root, self.repository)

        with self.assertRaises(CodingError) as caught:
            unwired.release_authorised("job-1")

        self.assertEqual(
            caught.exception.details["reason_code"],
            "durable_outcome_unavailable",
        )
        self.assertTrue(allocated.path.is_dir())

    def test_an_unreadable_outcome_source_refuses(self) -> None:
        from alx.providers.coding_worktree import CodingWorktreeAllocator

        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")

        def explode(_job_id: str) -> str:
            raise RuntimeError("goal store unavailable")

        broken = CodingWorktreeAllocator(self.root, self.repository, explode)
        with self.assertRaises(CodingError) as caught:
            broken.release_authorised("job-1")

        self.assertEqual(
            caught.exception.details["reason_code"],
            "durable_outcome_unreadable",
        )
        self.assertTrue(allocated.path.is_dir())

    def test_a_genuine_success_still_releases(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")
        self.finished("job-1", "succeeded")

        released = allocator.release_authorised("job-1")

        self.assertTrue(released["released"])
        self.assertFalse(allocated.path.exists())


class BranchNamesEndingInDigitsStayReleasable(Repository):
    """D-029 naming is unchanged; only how it is checked afterwards changed.

    The previous check parsed a trailing `-N` off the final branch name to
    recover the allocation attempt. That is ambiguous by construction: `fix-2`
    is a perfectly good base name Core may choose on the first attempt, and it
    is indistinguishable by inspection from `fix` suffixed on the second. The
    first case was refused as a suffix mismatch even though nothing was wrong.

    The attempt is now recorded at allocation time, so both cases are exact.
    """

    def test_a_base_branch_ending_in_two_releases_on_the_first_attempt(
        self,
    ) -> None:
        """`fix-2` chosen by Core, no collision. The reported defect."""
        allocator = self.allocator()
        allocated = allocator.allocate("job-1", "fix-2")
        allocator.record_outcome("job-1", "succeeded")
        self.finished("job-1", "succeeded")

        self.assertEqual(allocated.branch, "fix-2")
        self.assertEqual(allocated.slot, "job-1")
        self.assertEqual(allocated.attempt, 1)

        released = allocator.release_authorised("job-1")

        self.assertTrue(released["released"])
        self.assertFalse(allocated.path.exists())

    def test_a_collision_producing_the_same_name_also_releases(self) -> None:
        """`fix` suffixed to `fix-2` on the second attempt."""
        git(self.repository, "branch", "fix")
        allocator = self.allocator()
        allocated = allocator.allocate("job-1", "fix")
        allocator.record_outcome("job-1", "succeeded")
        self.finished("job-1", "succeeded")

        self.assertEqual(allocated.branch, "fix-2")
        self.assertEqual(allocated.slot, "job-1-2")
        self.assertEqual(allocated.attempt, 2)

        released = allocator.release_authorised("job-1")

        self.assertTrue(released["released"])
        self.assertFalse(allocated.path.exists())

    def test_the_two_cases_are_distinguished_by_recorded_attempt(self) -> None:
        """Identical branch names, different allocations, both provable."""
        first = self.allocator().allocate("job-a", "fix-2")
        git(self.repository, "branch", "other")
        second = self.allocator().allocate("job-b", "other")

        self.assertEqual(first.branch, "fix-2")
        self.assertEqual(first.attempt, 1)
        self.assertEqual(second.branch, "other-2")
        self.assertEqual(second.attempt, 2)

        allocator = self.allocator()
        first_record = allocator.read_record("job-a")
        second_record = allocator.read_record("job-b")
        # The stored base name plus the attempt rebuild each branch exactly,
        # with no parsing of the final name.
        self.assertEqual(first_record["base_branch"], "fix-2")
        self.assertEqual(first_record["attempt"], 1)
        self.assertEqual(second_record["base_branch"], "other")
        self.assertEqual(second_record["attempt"], 2)

    def test_other_numeric_endings_release_normally(self) -> None:
        for job_id, branch in (
            ("job-a", "release-10"),
            ("job-b", "v1-3"),
            ("job-c", "fix-99"),
        ):
            with self.subTest(branch=branch):
                allocator = self.allocator()
                allocated = allocator.allocate(job_id, branch)
                allocator.record_outcome(job_id, "succeeded")
                self.finished(job_id, "succeeded")

                self.assertEqual(allocated.branch, branch)
                self.assertTrue(
                    allocator.release_authorised(job_id)["released"]
                )

    def test_a_tampered_attempt_still_refuses(self) -> None:
        """Recording the attempt must not become a way around the check."""
        allocator = self.allocator()
        allocated = allocator.allocate("job-1", "fix-2")
        allocator.record_outcome("job-1", "succeeded")
        self.finished("job-1", "succeeded")

        path = allocator.root / "job-1.allocation.json"
        record = json.loads(path.read_text())
        record["attempt"] = 2
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-1")

        self.assertEqual(
            caught.exception.details["detail"], "branch_slot"
        )
        self.assertTrue(allocated.path.is_dir())


class ForeignWorktreesAreNotAlxOrphans(Repository):
    """Orphan discovery needs positive provenance, not circumstantial fit.

    "Linked worktree of this repository, under the configured root, with no
    allocation record" described a manually created worktree just as well as an
    AL/X one. Reporting somebody else's directory as an AL/X orphan invites
    acting on a directory D-030 grants no authority over.

    AL/X now claims each slot before git creates anything in it, and only a
    claimed slot can be an orphan. The claim is written first and the record
    last, so the record-write-failure gap this exists to expose stays visible.
    """

    def _foreign_worktree(self, name: str, branch: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        git(self.repository, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def test_a_manually_created_worktree_is_not_an_orphan(self) -> None:
        """The exact reported defect."""
        allocator = self.allocator()
        foreign = self._foreign_worktree("someones-work", "their-branch")

        self.assertEqual(allocator.orphan_worktrees(), ())
        # Still there, untouched: not reporting it is not the same as hiding it.
        self.assertTrue(foreign.is_dir())
        self.assertIn(str(foreign), self.worktrees())

    def test_a_foreign_worktree_named_like_a_job_is_still_not_an_orphan(
        self,
    ) -> None:
        """Right shape, right place, no claim."""
        allocator = self.allocator()
        self._foreign_worktree("job-1", "looks-official")

        self.assertEqual(allocator.orphan_worktrees(), ())

    def test_a_foreign_worktree_is_not_released_either(self) -> None:
        allocator = self.allocator()
        foreign = self._foreign_worktree("job-1", "their-branch")
        self.finished("job-1", "succeeded")

        with self.assertRaises(CodingError):
            allocator.release_authorised("job-1")

        self.assertTrue(foreign.is_dir())

    def test_an_alx_orphan_is_still_discovered(self) -> None:
        """The claim makes the record-write-failure case findable."""
        from alx.providers import coding_worktree as module

        allocator = self.allocator()
        with unittest.mock.patch.object(
            module.CodingWorktreeAllocator, "_write_record",
            lambda self, allocated: (_ for _ in ()).throw(OSError("full")),
        ):
            with self.assertRaises(CodingError):
                allocator.allocate("job-1")

        orphans = allocator.orphan_worktrees()

        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["slot"], "job-1")
        self.assertEqual(orphans[0]["job_id"], "job-1")
        self.assertTrue(orphans[0]["claimed_at"])

    def test_alx_and_foreign_worktrees_are_distinguished(self) -> None:
        """Both present; only ours is reported."""
        from alx.providers import coding_worktree as module

        allocator = self.allocator()
        self._foreign_worktree("not-ours", "theirs")
        with unittest.mock.patch.object(
            module.CodingWorktreeAllocator, "_write_record",
            lambda self, allocated: (_ for _ in ()).throw(OSError("full")),
        ):
            with self.assertRaises(CodingError):
                allocator.allocate("job-1")

        orphans = allocator.orphan_worktrees()

        self.assertEqual([item["slot"] for item in orphans], ["job-1"])

    def test_a_claim_does_not_authorise_release(self) -> None:
        """Provenance says AL/X made it, never that a job succeeded in it."""
        from alx.providers import coding_worktree as module

        allocator = self.allocator()
        with unittest.mock.patch.object(
            module.CodingWorktreeAllocator, "_write_record",
            lambda self, allocated: (_ for _ in ()).throw(OSError("full")),
        ):
            with self.assertRaises(CodingError):
                allocator.allocate("job-1")
        # Even with a durable success, a claimed-but-unrecorded worktree has no
        # allocation record and cannot be released.
        self.finished("job-1", "succeeded")

        with self.assertRaises(CodingError) as caught:
            allocator.release_authorised("job-1")

        self.assertEqual(
            caught.exception.details["reason_code"], "allocation_record_missing"
        )
        self.assertTrue((allocator.root / "job-1").is_dir())

    def test_a_released_worktree_leaves_no_claim_behind(self) -> None:
        allocator = self.allocator()
        allocated = allocator.allocate("job-1")
        allocator.record_outcome("job-1", "succeeded")
        self.finished("job-1", "succeeded")

        allocator.release_authorised("job-1")

        self.assertIsNone(allocator.read_claim("job-1"))
        self.assertEqual(allocator.orphan_worktrees(), ())
        self.assertFalse(allocated.path.exists())

    def test_a_withdrawn_claim_does_not_block_the_next_attempt(self) -> None:
        """A collision withdraws its claim so the slot is not lost."""
        git(self.repository, "branch", "alx/coding/job-1")

        allocated = self.allocator().allocate("job-1")

        self.assertEqual(allocated.slot, "job-1-2")
        # The first attempt claimed job-1, failed to create it, and withdrew.
        self.assertIsNone(self.allocator().read_claim("job-1"))
        self.assertFalse((self.root / "job-1").exists())


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
