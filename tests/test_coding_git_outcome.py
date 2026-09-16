"""A coding job returns its repair as a branch and a commit SHA, end to end.

`tests/test_coding_git_workspace.py` proves the git mechanism in isolation:
which argv can run, what may be staged, when a commit is refused. This file
covers the other half — that a job dispatched through the ordinary capability
path actually uses it, and that what Core receives is a branch and a SHA rather
than a dirty worktree it has to interpret.

Everything here goes through the registry, broker and Safety Gate the way a
real job does, so nothing proved here depends on calling the agent directly.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.coding import (  # noqa: E402
    CODING_EXECUTE_PERMISSION,
    build_coding_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import CapabilityCall, CapabilityResultState  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.coding import RUN_CODING_TASK  # noqa: E402

from test_coding_agent import (  # noqa: E402
    FIXTURE_BRANCH,
    NOW,
    PlanningModel,
    RecordingSession,
    _FIXED,
    _allocator,
    _git,
    _worktree,
)


class GitOutcome(unittest.TestCase):
    """One real worktree, dispatched the way Core dispatches a coding job."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name)
        self.root = _worktree(self.parent)

    def run_job(self, session, **arguments):
        # D-031: the job is allocated an isolated worktree cut from `self.root`,
        # which is the canonical repository here. `worktree` is no longer an
        # argument, so a test that still passes one is naming the repository.
        arguments.pop("worktree", None)
        self.allocator = _allocator(self.parent, self.root)
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: "call-1",
            session=session, reviewer=PlanningModel(),
            allocator=self.allocator,
        )
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )
        result = broker.dispatch(
            CapabilityCall("call-1", RUN_CODING_TASK, arguments),
            AuthorityContext(
                "friedl", frozenset({CODING_EXECUTE_PERMISSION}), NOW
            ),
        ).result
        # Where the job actually worked, for the assertions that read a file or
        # a branch back out of it.
        reported = (result.values or {}).get("worktree", "")
        self.job_root = Path(reported) if reported else None
        return result

    def git(self, *argv: str) -> str:
        """Git in the canonical repository: refs are shared with the worktree."""
        return subprocess.run(
            ["git", *argv], cwd=self.root, check=True,
            capture_output=True, text=True,
        ).stdout

    def job_git(self, *argv: str) -> str:
        """Git in the job's own worktree, where its HEAD and status live."""
        return subprocess.run(
            ["git", *argv], cwd=self.job_root, check=True,
            capture_output=True, text=True,
        ).stdout


class ASuccessfulJobReturnsABranchAndASha(GitOutcome):
    """The capability's reason for existing."""

    def test_a_collision_commits_on_the_suffix_selected_branch(self) -> None:
        self.git("branch", "repair/add")
        occupied_ref = self.git("rev-parse", "repair/add").strip()

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["branch"], "repair/add-2")
        self.assertEqual(result.values["commit"]["branch"], "repair/add-2")
        self.assertEqual(self.git("rev-parse", "repair/add").strip(), occupied_ref)

    def test_the_outcome_carries_branch_commit_sha_and_files(self) -> None:
        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        values = result.values
        self.assertEqual(values["branch"], "repair/add")
        self.assertEqual(
            values["commit_sha"],
            self.git("rev-parse", "repair/add").strip(),
        )
        self.assertEqual(list(values["commit"]["committed_files"]), ["app.py"])
        self.assertTrue(values["commit"]["worktree_clean"])

    def test_the_commit_exists_on_the_repair_branch(self) -> None:
        self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        # The job's own worktree is the one on the repair branch; the
        # canonical checkout stayed where it was, which is the D-031 property.
        self.assertEqual(
            self.job_git("rev-parse", "--abbrev-ref", "HEAD").strip(), "repair/add"
        )
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").strip(), FIXTURE_BRANCH
        )
        self.assertEqual(
            self.git("log", "-1", "--pretty=%s", "repair/add").strip(),
            "repair addition",
        )
        self.assertIn(
            "app.py",
            self.git("show", "--name-only", "--pretty=", "repair/add"),
        )

    def test_the_baseline_names_the_commit_the_job_started_from(self) -> None:
        """What "where the job started" means changed with D-031.

        Under D-029 the job switched branches inside a worktree it inherited,
        so its starting branch was a fact about somebody else's checkout, and
        reporting the repair branch there was false evidence — reproduced on
        2026-09-12.

        A D-031 job has no such prior branch: its worktree is created already
        on its own branch, cut from the repository's HEAD. The branch in the
        baseline is therefore the job's own, which is the truth about the
        worktree being described. The commit it starts from is the fact that
        still ties the job to the repository, and that is asserted here.
        """
        start_sha = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        self.assertEqual(result.values["baseline"]["head_sha"], start_sha)
        self.assertEqual(result.values["baseline"]["branch"], "repair/add")
        self.assertEqual(result.values["commit"]["branch"], "repair/add")
        # The canonical checkout never moved.
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").strip(), FIXTURE_BRANCH
        )

    def test_the_baseline_proves_where_the_job_started(self) -> None:
        before = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        baseline = result.values["baseline"]
        self.assertEqual(baseline["head_sha"], before)
        self.assertTrue(baseline["clean"])
        self.assertEqual(list(baseline["inherited_dirty"]), [])


class DeletionsReachTheCommit(GitOutcome):
    """A Law 0 cleanup is exactly the repair V1 previously could not commit."""

    def test_a_job_that_deletes_a_file_commits_the_deletion(self) -> None:
        class DeletingSession(RecordingSession):
            def run_session(self, request, briefing):
                result = super().run_session(request, briefing)
                (Path(request.worktree) / "superseded.py").unlink()
                return result

        (self.root / "superseded.py").write_text("old path\n", encoding="utf-8")
        _git(self.root, "add", "superseded.py")
        _git(self.root, "commit", "-m", "add superseded path")

        result = self.run_job(
            DeletingSession(edits={"app.py": _FIXED}),
            task="repair and remove the superseded path",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition and remove superseded path",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(
            sorted(result.values["commit"]["committed_files"]),
            ["app.py", "superseded.py"],
        )
        # Gone from the branch the job committed on. The canonical checkout
        # still has it, because nothing merged the repair.
        self.assertNotIn(
            "superseded.py", self.git("ls-tree", "--name-only", "repair/add")
        )
        self.assertIn(
            "superseded.py", self.git("ls-tree", "--name-only", "HEAD")
        )

    def test_an_inherited_deletion_is_not_committed(self) -> None:
        """Deleted before the job started, so not the job's to remove."""
        (self.root / "theirs.py").write_text("theirs\n", encoding="utf-8")
        _git(self.root, "add", "theirs.py")
        _git(self.root, "commit", "-m", "add theirs")
        (self.root / "theirs.py").unlink()

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(list(result.values["commit"]["committed_files"]), ["app.py"])
        # Still tracked at HEAD, still missing on disk: untouched either way.
        self.assertIn("theirs.py", self.git("ls-tree", "--name-only", "HEAD"))


class InheritedDirtNeverReachesTheJob(GitOutcome):
    """D-031 turned this from a staging rule into structural isolation.

    These tests used to describe a job running in a worktree it did not own,
    where somebody else's uncommitted work sat in the same directory and the
    commit logic had to be careful not to sweep it in. That care is still
    there and still tested in `test_coding_git_workspace.py`, against the
    staging code directly.

    What changed is that a job no longer starts from a dirty tree at all. Its
    worktree is cut from the repository's committed HEAD, so the dirt it used
    to have to step around is not present to step around. Both halves are
    asserted: the job commits only its own file, and the other work is still
    sitting untouched in the checkout afterwards.
    """

    def test_dirt_in_the_checkout_is_not_visible_to_the_job(self) -> None:
        # Deliberately not a test module: dirtying one would make AL/X's own
        # verification fail and the job would be failed for that reason
        # instead of the one under test.
        theirs = "# somebody else was working here\n"
        (self.root / "notes.txt").write_text(theirs, encoding="utf-8")

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(list(result.values["commit"]["committed_files"]), ["app.py"])
        self.assertNotIn(
            "notes.txt",
            self.git("show", "--name-only", "--pretty=", "repair/add"),
        )
        # There was no dirt to inherit, and the job's own tree ends clean.
        self.assertEqual(tuple(result.values["baseline"]["inherited_dirty"]), ())
        self.assertTrue(result.values["commit"]["worktree_clean"])

    def test_the_other_work_is_left_exactly_as_it_was_found(self) -> None:
        theirs = "# somebody else was working here\n"
        (self.root / "notes.txt").write_text(theirs, encoding="utf-8")

        self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual((self.root / "notes.txt").read_text(), theirs)
        self.assertIn("notes.txt", self.git("status", "--porcelain"))

    def test_a_stale_edit_in_the_checkout_does_not_become_the_job_s(self) -> None:
        """The job commits what it wrote, from the committed baseline."""
        stale = "# stale edit\n"
        (self.root / "app.py").write_text(stale, encoding="utf-8")

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(tuple(result.values["baseline"]["inherited_dirty"]), ())
        self.assertEqual(list(result.values["commit"]["committed_files"]), ["app.py"])
        # The job's own worktree holds the repair; the checkout still holds the
        # stale edit its owner left there.
        self.assertEqual((self.job_root / "app.py").read_text(), _FIXED)
        self.assertEqual((self.root / "app.py").read_text(), stale)


class CommittingIsRefusedRatherThanWidened(GitOutcome):
    """Fail closed: no commit is better than the wrong commit."""

    def test_someone_elses_staged_file_cannot_reach_the_job_s_commit(self) -> None:
        """D-031 moved this from a refusal to an impossibility.

        Staging an unrelated file used to poison the job's own index, because
        the job shared it. `_refuse_foreign_staged_state` caught that and
        failed the job closed, and still does — proved directly against the
        staging code in `test_coding_git_workspace.py`.

        A linked worktree has its own index, so the checkout's staged file is
        not in the job's index to be caught. The job succeeds, its commit
        contains only its own file, and the staged work is left exactly as its
        owner left it.
        """
        (self.root / "theirs.py").write_text("not this job's\n", encoding="utf-8")
        _git(self.root, "add", "theirs.py")
        before = self.git("rev-parse", "HEAD").strip()

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(list(result.values["commit"]["committed_files"]), ["app.py"])
        self.assertNotIn(
            "theirs.py",
            self.git("show", "--name-only", "--pretty=", "repair/add"),
        )
        # The checkout did not move, and their staged file is still staged.
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        self.assertIn("A  theirs.py", self.git("status", "--porcelain"))

    def test_a_failed_job_is_not_committed(self) -> None:
        """Evidence Core still has to judge must not read as a finished repair."""
        before = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            RecordingSession(
                edits={"app.py": _FIXED}, completed=False,
                failure_code="session_failed",
            ),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)

    def test_a_job_that_ran_no_verification_is_not_committed(self) -> None:
        """D-029 authorises a commit after the job passed its verification.

        A job that ran *no* verification has not passed it; it skipped it.
        Before this was fixed, the status derivation only failed a job on
        `tests_run and tests_passed is False`, so a job where no candidate
        command survived the allowlist reached "succeeded" with tests_run
        False and was committed. An unverified commit reads downstream exactly
        like a verified one, which is what makes the gap worth closing.
        """
        from unittest.mock import patch

        from alx.providers import coding_agent as agent_module

        before = self.git("rev-parse", "HEAD").strip()
        with patch.object(
            agent_module.CodingAgent, "_verification_commands", return_value=()
        ):
            result = self.run_job(
                RecordingSession(edits={"app.py": _FIXED}),
                task="repair the addition",
                worktree=str(self.root),
                repair_branch="repair/add",
                commit_message="repair addition",
            )

        self.assertFalse(result.values["tests_run"])
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        # Said plainly, so Core knows why there is no SHA rather than having
        # to infer it from an absent field.
        self.assertIn(
            "unverified_not_committed", result.values["unresolved_issues"]
        )
        # The work is not thrown away: it is still there as a diff.
        self.assertEqual(list(result.values["files_changed"]), ["app.py"])

    def test_a_job_whose_tests_failed_is_not_committed(self) -> None:
        """The neighbouring case, so the two cannot be confused."""
        before = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            # Leaves app.py broken, so the fixture's own test fails.
            RecordingSession(edits={"app.py": "def add(a, b):\n    return a * b\n"}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)

    def test_a_job_changing_more_files_than_the_bound_is_not_committed(self) -> None:
        """Finding 2 of the 2026-09-12 re-review, confirmed by inspection.

        `_files_changed` clipped to MAX_REPORTED_FILES and `_authorised_paths`
        then tested for more than MAX_STAGED_FILES — the same number — so the
        bound could never fire. A job changing sixty files staged the first
        fifty and reported a complete repair. The untruncated count now
        decides whether a commit is possible at all.
        """
        from alx.contracts.coding import MAX_STAGED_FILES

        # app.py is repaired too, so the job's own tests pass and the file
        # bound is what this test exercises rather than a test failure.
        edits = {"app.py": _FIXED}
        edits.update({
            f"generated_{index:03d}.py": f"value = {index}\n"
            for index in range(MAX_STAGED_FILES + 10)
        })
        before = self.git("rev-parse", "HEAD").strip()

        result = self.run_job(
            RecordingSession(edits=edits),
            task="generate many modules",
            worktree=str(self.root),
            repair_branch="repair/many",
            commit_message="generate modules",
        )

        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        self.assertIn(
            "too_many_files_to_commit", result.values["unresolved_issues"]
        )

    def test_commit_authorisation_uses_the_complete_inherited_dirt(self) -> None:
        """Finding 1: the bounded status path must not decide what may stage.

        `preexisting_dirty` comes from evidence clipped at 16,000 characters.
        A clipped inherited path that reappears in the post-session status
        would read as job-owned. Authorisation now uses the baseline reader's
        complete listing.
        """
        import inspect

        from alx.providers.coding_agent import CodingAgent

        source = inspect.getsource(CodingAgent._run)
        self.assertIn("baseline_dirty", source)
        # The commit call takes the complete sets, not the reporting ones.
        self.assertIn("complete_files,", source)
        self.assertIn("baseline_dirty,", source)

    def test_a_branch_that_cannot_be_created_fails_before_the_session(self) -> None:
        """A session must never write to a branch nobody asked for."""
        session = RecordingSession(edits={"app.py": _FIXED})
        result = self.run_job(
            session,
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="--force",
            commit_message="repair addition",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "git_refused")
        self.assertEqual(session.calls, [])

    def test_a_commit_message_without_a_branch_is_rejected_as_an_argument(self) -> None:
        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            commit_message="repair addition",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "arguments_unusable")


class TheCapabilityWithoutGitIsUnchanged(GitOutcome):
    """Asking for no branch leaves the job exactly as it was."""

    def test_a_job_without_a_branch_makes_no_commit(self) -> None:
        before = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
        )
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertNotIn("commit", result.values)
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        # The change is still there as a diff, which is what it used to be.
        self.assertEqual(list(result.values["files_changed"]), ["app.py"])

    def test_a_job_without_a_branch_stays_on_the_original_branch(self) -> None:
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
        )
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").strip(), branch
        )


class TheSessionStillHasNoGitAuthority(GitOutcome):
    """Git moved to AL/X's side of the boundary, not into the session's."""

    def test_the_briefing_still_forbids_the_session_from_committing(self) -> None:
        session = RecordingSession(edits={"app.py": _FIXED})
        self.run_job(
            session,
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        _request, briefing = session.calls[0]
        self.assertIn("Do not commit, push, merge, deploy", briefing)
        self.assertIn("repair/add", briefing)

    def test_the_session_cannot_run_a_git_command_of_its_own(self) -> None:
        """The sandbox denies .git outright; this states the intent alongside."""
        from alx.providers.coding_process import command_permitted

        for argv in (
            ["git", "commit", "-m", "mine"],
            ["git", "switch", "-c", "mine"],
            ["git", "add", "-A"],
            ["git", "push"],
        ):
            self.assertFalse(
                command_permitted(argv, self.root), " ".join(argv)
            )


if __name__ == "__main__":
    unittest.main()
