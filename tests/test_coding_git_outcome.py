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
    NOW,
    PlanningModel,
    RecordingSession,
    _FIXED,
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
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: "call-1",
            session=session, reviewer=PlanningModel(),
        )
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )
        return broker.dispatch(
            CapabilityCall("call-1", RUN_CODING_TASK, arguments),
            AuthorityContext(
                "friedl", frozenset({CODING_EXECUTE_PERMISSION}), NOW
            ),
        ).result

    def git(self, *argv: str) -> str:
        return subprocess.run(
            ["git", *argv], cwd=self.root, check=True,
            capture_output=True, text=True,
        ).stdout


class ASuccessfulJobReturnsABranchAndASha(GitOutcome):
    """The capability's reason for existing."""

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
        self.assertEqual(values["commit_sha"], self.git("rev-parse", "HEAD").strip())
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
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").strip(), "repair/add"
        )
        self.assertEqual(
            self.git("log", "-1", "--pretty=%s").strip(), "repair addition"
        )
        self.assertIn("app.py", self.git("show", "--name-only", "--pretty=", "HEAD"))

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


class InheritedDirtIsNeverCommitted(GitOutcome):
    """A job runs in a worktree it does not own."""

    def test_an_inherited_dirty_file_is_not_in_the_commit(self) -> None:
        # Deliberately not a test module: dirtying one would make AL/X's own
        # verification fail and the job would be failed for that reason
        # instead of the one under test.
        (self.root / "notes.txt").write_text(
            "# somebody else was working here\n", encoding="utf-8"
        )

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
            "notes.txt", self.git("show", "--name-only", "--pretty=", "HEAD")
        )
        # Reported, so Core knows the tree is not clean and why.
        self.assertIn("notes.txt", result.values["baseline"]["inherited_dirty"])
        self.assertFalse(result.values["commit"]["worktree_clean"])

    def test_the_inherited_file_is_left_exactly_as_it_was_found(self) -> None:
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

    def test_an_inherited_file_the_job_itself_rewrites_is_committed(self) -> None:
        """Ownership is by what the job wrote, not by what was dirty."""
        (self.root / "app.py").write_text("# stale edit\n", encoding="utf-8")

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertIn("app.py", result.values["baseline"]["inherited_dirty"])
        self.assertEqual(list(result.values["commit"]["committed_files"]), ["app.py"])
        self.assertEqual((self.root / "app.py").read_text(), _FIXED)


class CommittingIsRefusedRatherThanWidened(GitOutcome):
    """Fail closed: no commit is better than the wrong commit."""

    def test_a_pre_staged_unrelated_file_refuses_the_commit(self) -> None:
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

        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "unrelated_changes_staged")
        # No commit was created, and their staged file is still staged.
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
